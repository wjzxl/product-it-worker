#!/usr/bin/env python3
"""上线就绪体检：**生产档位下不允许带着宽松的治理开关上线**。

为什么需要它：把"严格 vs 开发便利"这个取舍收拢到 `PLATFORM_ENV` 之后，还差一步——
让"prod 档位 + 宽松开关"这件事**不可能悄悄发生**。默认值可以被显式环境变量覆盖，
覆盖错了没人会发现；这个脚本就是那道发布卡点。

判据（与 `service_auth.strict_expectations()` 同一份口径，避免"体检说合格、服务实际是松的"）：

| 档位 | 要求 |
|---|---|
| dev | 只提示，不拦（本机开发） |
| staging/prod | `SERVICE_AUTH_MODE=token` 且有凭据；`GATE_DECISION_VERIFY=block`；`ENG_GATES_MODE=block`；`CHECKPOINTER=postgres`；`ENG_GATES_REQUIRED=1`；`TRUST_PROXY_AUTH=1`；告警与 IM 密钥齐全 |

用法：
    python3 platform/scripts/check_deployment_readiness.py                    # 按当前环境变量体检
    python3 platform/scripts/check_deployment_readiness.py --env-file .env.prod
    python3 platform/scripts/check_deployment_readiness.py --selftest         # 自检（CI 用）

退出码：0 = 可以上线；1 = 存在阻断项（prod/staging）；2 = 脚本自身问题。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM / "services" / "_shared"))

import service_auth  # noqa: E402  （真源模块，脚本直接复用同一份口径）

# 不可逆的治理入口：不随档位放宽（宽松放行没有任何真实收益，失败后果却不可逆）
NON_NEGOTIABLE = [
    ("TOOLS_ALLOW_UNGATED", "0", "工具调用不得由请求体自述'免人审'"),
    ("TOOLS_ALLOW_CLIENT_ROLE", "0", "工具调用不得由请求体自述岗位（受管控部署）"),
]

SECRETS_REQUIRED_IN_PROD = [
    ("SERVICE_AUTH_TOKEN", "服务间令牌；缺它服务会拒绝启动（fail-closed）"),
    ("ALERT_WEBHOOK_SECRET", "Alertmanager → task-center 的共享密钥"),
]


def load_env_file(path: Path) -> int:
    """把 KEY=VALUE 读进 os.environ（已存在的真实环境变量优先）。"""
    if not path.exists():
        print(f"FAIL: 找不到 env 文件 {path}")
        return 2
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n


def evaluate() -> tuple[list[str], list[str], str]:
    """返回 (阻断项, 提醒项, 档位)。"""
    env = service_auth.platform_env()
    expect = service_auth.strict_expectations()
    blockers: list[str] = []
    notes: list[str] = []

    def want(name: str, expected: str, why: str) -> None:
        actual = os.getenv(name, "")
        if actual == "":
            actual = f"（未设置，生效值 {expect.get(name, '?')}）"
        effective = expect.get(name, actual)
        if effective != expected:
            (blockers if env in ("staging", "prod") else notes).append(
                f"{name}={effective}（要求 {expected}）：{why}")

    want("SERVICE_AUTH_MODE", "token", "所有写接口都要求服务令牌，否则端口可达即等于有全部写权限")
    want("GATE_DECISION_VERIFY", "block", "恢复人审挂起必须能对上审批台里的真实决策，不接受自述批准")
    want("ENG_GATES_MODE", "block", "工程闸门在关键条件缺失时要真的阻断，而不是只记录")
    want("ENG_GATES_REQUIRED", "1", "闸门服务不可用时视为未通过（拿不到证据就不推进）")
    want("CHECKPOINTER", "postgres", "进程重启后仍能恢复等待中的人审（memory 会丢状态）")
    # 代理头信任**必须显式声明**（0 或 1 都合法，未声明不行）：
    # 它只在"入口剥离了同名头"的前提下成立，默认替部署方假设它做了，就是自欺。
    if os.getenv("TRUST_PROXY_AUTH", "") == "":
        (blockers if env in ("staging", "prod") else notes).append(
            "TRUST_PROXY_AUTH 未显式声明：请选一种人类入口鉴权方式——"
            "SSO 代理注入 X-Platform-User（=1，且入口必须剥离客户端自带的同名头），"
            "或人类走 Bearer 令牌（=0）")

    for name, expected, why in NON_NEGOTIABLE:
        actual = os.getenv(name, "0")
        if actual != expected:
            blockers.append(f"{name}={actual}（要求 {expected}）：{why}")

    for name, why in SECRETS_REQUIRED_IN_PROD:
        if not os.getenv(name, ""):
            (blockers if env in ("staging", "prod") else notes).append(f"{name} 未配置：{why}")

    if not (os.getenv("IM_CALLBACK_SIGNING_KEY") or os.getenv("FEISHU_VERIFY_TOKEN")):
        (blockers if env in ("staging", "prod") else notes).append(
            "IM 回调签名密钥未配置（IM_CALLBACK_SIGNING_KEY / FEISHU_VERIFY_TOKEN）："
            "飞书卡片按钮会失效（503）——不是安全问题，但功能不可用")
    posture = service_auth.security_posture("deployment")
    return blockers, notes + [f"姿态提示：{w}" for w in posture["warnings"] if "未配置密钥" not in w], env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", help="从文件加载环境变量（不覆盖已存在的）")
    ap.add_argument("--selftest", action="store_true", help="自检：prod 档位应拦、dev 档位应放")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if args.env_file:
        n = load_env_file(Path(args.env_file))
        if n == 2:
            return 2
        print(f"已从 {args.env_file} 加载 {n} 个变量（已存在的环境变量优先）")

    blockers, notes, env = evaluate()
    print(f"部署档位 PLATFORM_ENV={env}")
    for line in notes:
        print(f"  · {line}")
    if blockers:
        print(f"\n❌ 存在 {len(blockers)} 项上线阻断：")
        for line in blockers:
            print(f"  ✗ {line}")
        print("\n修复：在 .env.prod 里补齐（见 platform/.env.prod.example），或明确降级 PLATFORM_ENV。")
        return 1
    print("\n✅ 上线就绪：治理开关均为严格档位，凭据齐全。")
    return 0


def _selftest() -> int:
    import subprocess

    cases = [
        ("dev 宽松（应放行）",
         {"PLATFORM_ENV": "dev", "SERVICE_AUTH_MODE": "off", "GATE_DECISION_VERIFY": "warn",
          "ENG_GATES_MODE": "warn", "CHECKPOINTER": "memory", "TOOLS_ALLOW_UNGATED": "0",
          "TOOLS_ALLOW_CLIENT_ROLE": "0"}, 0),
        ("prod 严格齐全（应放行）",
         {"PLATFORM_ENV": "prod", "SERVICE_AUTH_MODE": "token", "SERVICE_AUTH_TOKEN": "t",
          "GATE_DECISION_VERIFY": "block", "ENG_GATES_MODE": "block", "ENG_GATES_REQUIRED": "1",
          "CHECKPOINTER": "postgres", "TRUST_PROXY_AUTH": "1", "ALERT_WEBHOOK_SECRET": "a",
          "IM_CALLBACK_SIGNING_KEY": "i", "TOOLS_ALLOW_UNGATED": "0",
          "TOOLS_ALLOW_CLIENT_ROLE": "0"}, 0),
        ("prod 但凭据缺失（应拦）",
         {"PLATFORM_ENV": "prod", "SERVICE_AUTH_MODE": "off", "GATE_DECISION_VERIFY": "warn",
          "ENG_GATES_MODE": "warn", "TOOLS_ALLOW_UNGATED": "0", "TOOLS_ALLOW_CLIENT_ROLE": "0"}, 1),
        ("prod 齐全但 TRUST_PROXY_AUTH 未声明（应拦：不能替部署方假设）",
         {"PLATFORM_ENV": "prod", "SERVICE_AUTH_MODE": "token", "SERVICE_AUTH_TOKEN": "t",
          "GATE_DECISION_VERIFY": "block", "ENG_GATES_MODE": "block", "ENG_GATES_REQUIRED": "1",
          "CHECKPOINTER": "postgres", "ALERT_WEBHOOK_SECRET": "a", "IM_CALLBACK_SIGNING_KEY": "i",
          "TOOLS_ALLOW_UNGATED": "0", "TOOLS_ALLOW_CLIENT_ROLE": "0"}, 1),
        ("dev 但打开 allow_ungated（不可协商项，应拦）",
         {"PLATFORM_ENV": "dev", "TOOLS_ALLOW_UNGATED": "1", "TOOLS_ALLOW_CLIENT_ROLE": "0"}, 1),
    ]
    ok = True
    base = {k: v for k, v in os.environ.items() if not k.startswith(("PLATFORM_ENV", "SERVICE_AUTH",
                                                                    "GATE_DECISION", "ENG_GATES",
                                                                    "CHECKPOINTER", "TRUST_PROXY",
                                                                    "TOOLS_ALLOW", "ALERT_WEBHOOK",
                                                                    "IM_CALLBACK", "WEBHOOK_SECRET"))}
    for name, envs, expected in cases:
        env = {**base, **envs}
        env["PATH"] = os.environ["PATH"]
        proc = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, cwd=PLATFORM)
        got = proc.returncode
        mark = "✅" if got == expected else "❌"
        if got != expected:
            ok = False
        print(f"{mark} {name} → 退出码 {got}（期望 {expected}）")
    print("自检通过 ✅" if ok else "自检失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
