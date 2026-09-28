#!/usr/bin/env python3
"""从模板生成真实部署配置：**密钥自动生成，人只填人的东西**。

背景：`check_deployment_readiness.py` 能拦住"生产带着宽松开关上线"，但拦不住另一件事——
**人忘了填那 8 个变量**。而"要记住填 8 个变量"本身就是上一轮 22/33 接口裸奔的同一个病根。
所以这里把它变成一条命令：密钥类的自动生成（强随机、写盘即 600 权限），
只剩真正需要人来决定的（域名、GitLab Token、模型 Key）留给清单。

用法：
    python3 platform/scripts/init_prod_env.py --out platform/.env.prod          # 生成
    python3 platform/scripts/init_prod_env.py --out platform/.env.prod --check  # 只看还缺什么
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
TEMPLATE = PLATFORM / ".env.prod.example"

# 自动生成强随机的项（值是"共享密钥"性质，人编不出来也不该人编）
GENERATED = {
    "POSTGRES_PASSWORD": "数据库口令（会同时替换下面三条 DSN 里的占位值）",
    "SERVICE_AUTH_TOKEN": "服务间令牌",
    "WEBHOOK_SECRET": "GitLab webhook 密钥",
    "ALERT_WEBHOOK_SECRET": "告警 webhook 密钥",
    "IM_CALLBACK_SIGNING_KEY": "飞书卡片签名密钥",
    "AUTH_STATIC_TOKEN": "人类 API 静态令牌（AUTH_MODE=static 时用）",
    "LITELLM_MASTER_KEY": "模型网关主密钥",
}

# 必须由人提供、脚本无法代劳的项（空值或占位值都会点名）
HUMAN_REQUIRED = {
    "PLATFORM_DOMAIN": "对外域名（Caddy 自动签证书用）",
    "PUBLIC_BASE_URL": "对外基址（写给 GitLab/飞书的回调地址）",
    "OIDC_ISSUER": "SSO issuer（AUTH_MODE=oidc）",
    "OIDC_AUDIENCE": "SSO 客户端 ID",
    "GITLAB_URL": "GitLab 地址（内网或 gitlab.com）",
    "GITLAB_TOKEN": "GitLab 访问令牌",
    "QWEN_API_KEY": "通义千问 Key（或换成你们在用的模型 Key）",
}

# 占位值的三种形态：空、change-me 开头（模板习惯）、内含 example.com（示例域名/地址）。
# 只在**值内部**出现也算占位——`PLATFORM_DSN=host=...password=change-me-pg-password`
# 这种"看着填了其实没填"的情况必须被抓出来。
PLACEHOLDER = re.compile(r"(^$)|change-me|example\.com")


def _gen(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(32)}"


def generate(template: Path, out: Path, force: bool) -> int:
    if out.exists() and not force:
        print(f"FAIL: {out} 已存在（加 --force 覆盖；覆盖会换掉现有密钥，务必先备份）")
        return 1
    text = template.read_text(encoding="utf-8")
    replaced: list[str] = []
    pg_password = ""
    for key, why in GENERATED.items():
        prefix = "sk" if key.endswith("MASTER_KEY") else "pst"
        value = _gen(prefix)
        if key == "POSTGRES_PASSWORD":
            pg_password = value
        pattern = re.compile(rf"^{re.escape(key)}=.*$", flags=re.M)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text)
            replaced.append(f"{key}（{why}）")
        else:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
            replaced.append(f"{key}（{why}，模板里原本没有，已追加）")
    # 库口令只生成一次，同时替换三条连接串里的占位值（避免"改了密码忘了改 DSN"）
    if pg_password:
        text = text.replace("change-me-pg-password", pg_password)
    out.write_text(text, encoding="utf-8")
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)   # 600：密钥文件不该被同机其它账号读到
    print(f"已生成 {out}（权限 600），自动生成 {len(replaced)} 项：")
    for item in replaced:
        print(f"  · {item}")
    print("\n仍需人工填写（脚本无法代劳）：")
    for key, why in HUMAN_REQUIRED.items():
        print(f"  · {key}：{why}")
    print(f"\n下一步：填完后跑 `python3 {PLATFORM.name}/scripts/check_deployment_readiness.py "
          f"--env-file {out}`（不通过就不要部署）")
    return 0


def check(path: Path) -> int:
    if not path.exists():
        print(f"FAIL: {path} 不存在，先跑 init_prod_env.py 生成")
        return 1
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    # 用 search 而不是 match：占位值可能出现在值内部（`platform.example.com`、
    # `...password=change-me-pg-password`），只锚开头会放过这些"看着填了其实没填"的情况
    missing = [f"{k}（{why}）" for k, why in HUMAN_REQUIRED.items() if PLACEHOLDER.search(values.get(k, ""))]
    empty_generated = [k for k in GENERATED if PLACEHOLDER.search(values.get(k, ""))]
    for key in empty_generated:
        missing.append(f"{key}（共享密钥，应为自动生成值）")
    if missing:
        print(f"❌ {path} 还有 {len(missing)} 项未填：")
        for item in missing:
            print(f"  ✗ {item}")
        return 1
    print(f"✅ {path} 必填项齐全（治理开关另由 check_deployment_readiness.py 判定）")
    return 0


def selftest() -> int:
    """自检（CI 用）：生成 → 未填必须被拦 → 填齐必须放行。"""
    import tempfile

    out = Path(tempfile.mkdtemp(prefix="init-prod-env-")) / ".env.prod"
    assert generate(TEMPLATE, out, force=False) == 0
    mode = stat.S_IMODE(out.stat().st_mode)
    ok_mode = mode == 0o600
    print(f"{'✅' if ok_mode else '❌'} 密钥文件权限为 600（实际 {oct(mode)}）")

    text = out.read_text(encoding="utf-8")
    generated_ok = all(not PLACEHOLDER.search(v) for k, v in
                       ((line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")))
                       if k in GENERATED)
    print(f"{'✅' if generated_ok else '❌'} 自动生成的密钥都不是占位值")

    unfilled_blocked = check(out) == 1
    print(f"{'✅' if unfilled_blocked else '❌'} 未填齐时被拦下（exit 1）")

    for key in HUMAN_REQUIRED:
        text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}=real-{key.lower()}", text, flags=re.M)
    out.write_text(text, encoding="utf-8")
    filled_ok = check(out) == 0
    print(f"{'✅' if filled_ok else '❌'} 填齐后放行（exit 0）")
    ok = ok_mode and generated_ok and unfilled_blocked and filled_ok
    print("自检通过 ✅" if ok else "自检失败 ❌")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(PLATFORM / ".env.prod"))
    ap.add_argument("--template", default=str(TEMPLATE))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true", help="只检查还缺什么，不生成")
    ap.add_argument("--selftest", action="store_true", help="自检（CI 用）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    template = Path(args.template)
    out = Path(args.out)
    if args.check:
        return check(out)
    if not template.exists():
        print(f"FAIL: 找不到模板 {template}")
        return 1
    return generate(template, out, args.force)


if __name__ == "__main__":
    sys.exit(main())
