#!/usr/bin/env python3
"""工程闸门 CLI（WP2）：本地/CI 干跑规则，不依赖运行中的服务。

两种用法：
  1. **干跑规则自检**（CI 用）：`--selftest` 校验全部规则能被加载、含 critical 项、
     `unless/mode` 合法，并对内置样例断言"关键缺失必须阻断"；
  2. **按上下文判定**：`--code E1 --context-file ctx.json`（或 --task-ref 走服务端自动补齐）。

为什么要有 CLI：闸门规则是"规则即代码"，必须能在**不启动服务**的情况下被 CI 校验——
否则规则写错只能等到线上拦截时才发现。

用法：
    cd platform
    services/approval-gateway/.venv/bin/python scripts/check_gates.py --selftest
    services/approval-gateway/.venv/bin/python scripts/check_gates.py --code E1 --context-file /tmp/ctx.json
    services/approval-gateway/.venv/bin/python scripts/check_gates.py --code E1 --task-ref tsk_1 --url http://127.0.0.1:8001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "approval-gateway"))

from app import gate_rules  # noqa: E402

# CI 自检样例：4 项具备、critical 的 impact_scope 缺失 → 必须阻断
SELFTEST_CONTEXT_PARTIAL = {
    "prd_ref": "PRD v1",
    "sources": [{"kind": "chat", "ref": "feishu://x", "quote": "加个导出", "confirmed_by": "运营-张"}],
    "change_targets": ["src/a.py"],
    "rollback_plan": "切回上一版本",
}


def selftest() -> int:
    failures: list[str] = []
    codes = gate_rules.available_codes()
    if not codes:
        print("❌ 未找到任何工程闸门规则文件")
        return 1

    for code in codes:
        try:
            rule = gate_rules.load_rule(code)
        except gate_rules.GateRuleError as exc:
            failures.append(f"{code} 规则非法：{exc}")
            continue
        crit = [c.id for c in rule.checks if c.severity == "critical"]
        if not crit:
            failures.append(f"{code} 没有任何 critical 检查项（关键未知将无法阻断推进）")
        blocked = gate_rules.evaluate(rule, SELFTEST_CONTEXT_PARTIAL, mode_override="block")
        if code == "E1" and blocked["passed"]:
            failures.append("E1：critical 项缺失却判定通过——阻断语义失效")
        if code == "E1" and not blocked["blocked"]:
            failures.append("E1：mode=block 且存在缺口，却未标记 blocked")
        warn = gate_rules.evaluate(rule, SELFTEST_CONTEXT_PARTIAL, mode_override="warn")
        if warn["blocked"]:
            failures.append(f"{code}：warn 模式下不应阻断（灰度语义）")
        print(f"  {'✅' if not failures else '·'} {code} {rule.title}"
              f"｜检查项 {len(rule.checks)}（critical {len(crit)}）｜指纹 {rule.fingerprint()}")

    if failures:
        print("\n❌ 闸门自检未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n✅ 闸门自检通过（{len(codes)} 个规则）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="工程闸门 CLI（判定 / 自检）")
    parser.add_argument("--selftest", action="store_true", help="校验规则文件与阻断语义（CI 用）")
    parser.add_argument("--code", default="E1")
    parser.add_argument("--context-file", default="", help="上下文 JSON 文件")
    parser.add_argument("--task-ref", default="", help="走服务端时按任务自动补齐来源链")
    parser.add_argument("--url", default="", help="approval-gateway 地址；给了就走服务端并留痕")
    parser.add_argument("--mode", default="", choices=["", "off", "warn", "block"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    context = {}
    if args.context_file:
        context = json.loads(Path(args.context_file).read_text(encoding="utf-8"))

    if args.url:
        import httpx

        resp = httpx.post(
            f"{args.url.rstrip('/')}/eng-gates/{args.code.upper()}/check",
            json={"task_ref": args.task_ref, "context": context, "mode": args.mode or None,
                  "checked_by": "cli:check_gates"},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
    else:
        rule = gate_rules.load_rule(args.code.upper())
        result = gate_rules.evaluate(rule, context, mode_override=args.mode or None)

    if args.json:
        print(json.dumps(gate_rules.asdict_free(result), ensure_ascii=False, indent=2))
    else:
        print(gate_rules.render_gaps_text(result))
    return 0 if (result.get("passed") or not result.get("blocked")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
