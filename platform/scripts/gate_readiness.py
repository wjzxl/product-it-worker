#!/usr/bin/env python3
"""闸门切 block 的就绪度检查（D2）。

业务确认的切换门槛（写死在脚本里，避免"凭感觉切"）：
  1. warn 模式已运行 ≥ 2 周（首次 warn 检查的时间起算）；
  2. 真实样本 ≥ 20 次检查（演练库不算，需传真实库 DSN）；
  3. 误拦率可信且 < 10%（`feedback_stats.reliable == True` 且 rate < threshold）。

三项全绿才建议切 `ENG_GATES_MODE=block`；任一不满足就明确说"还差什么"。

用法：
    cd platform
    services/approval-gateway/.venv/bin/python scripts/gate_readiness.py
    ... --min-days 14 --min-checks 20
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = os.getenv(
    "PLATFORM_DB_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform"),
)
RULES_URL = "http://127.0.0.1:8001"


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def collect(url: str) -> dict:
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        try:
            rows = conn.execute(text(
                "select mode, blocked, feedback, checked_at from gate_checks order by checked_at asc"
            )).fetchall()
        except SQLAlchemyError:
            rows = []
    checks = [{"mode": r[0], "blocked": bool(r[1]), "feedback": r[2] or "", "at": _as_dt(r[3])} for r in rows]
    warn = [c for c in checks if c["mode"] == "warn"]
    blocked = [c for c in checks if c["blocked"]]
    judged = [c for c in blocked if c["feedback"]]
    false_pos = [c for c in judged if c["feedback"] == "false_positive"]
    return {
        "total": len(checks),
        "warn_count": len(warn),
        "first_warn_at": min((c["at"] for c in warn if c["at"]), default=None),
        "blocked": len(blocked),
        "judged": len(judged),
        "false_positive": len(false_pos),
        "rate": (len(false_pos) / len(judged)) if judged else None,
        "coverage": (len(judged) / len(blocked)) if blocked else 0.0,
    }


def assess(data: dict, min_days: int, min_checks: int, max_rate: float,
           min_coverage: float) -> dict:
    now = datetime.now(timezone.utc)
    first_warn = data["first_warn_at"]
    days = (now - first_warn).days if first_warn else 0
    gates = [
        {
            "name": f"warn 已运行 ≥ {min_days} 天",
            "ok": days >= min_days,
            "detail": f"当前 {days} 天" + (f"（首次 warn：{first_warn.date()}）" if first_warn else "（尚无 warn 记录）"),
        },
        {
            "name": f"真实样本 ≥ {min_checks} 次检查",
            "ok": data["total"] >= min_checks,
            "detail": f"当前 {data['total']} 次",
        },
        {
            "name": f"反馈覆盖率 ≥ {min_coverage:.0%}",
            "ok": data["coverage"] >= min_coverage and data["blocked"] > 0,
            "detail": f"当前 {data['coverage']:.0%}（已反馈 {data['judged']}/{data['blocked']} 次阻断）",
        },
        {
            "name": f"误拦率 < {max_rate:.0%}",
            "ok": data["rate"] is not None and data["rate"] < max_rate,
            "detail": "无反馈数据" if data["rate"] is None else f"当前 {data['rate']:.1%}",
        },
    ]
    return {"gates": gates, "ready": all(g["ok"] for g in gates)}


def main() -> int:
    parser = argparse.ArgumentParser(description="闸门切 block 的就绪度检查（D2，只读）")
    parser.add_argument("--dsn", default=DEFAULT_URL)
    parser.add_argument("--min-days", type=int, default=14)
    parser.add_argument("--min-checks", type=int, default=20)
    parser.add_argument("--max-rate", type=float, default=0.1)
    parser.add_argument("--min-coverage", type=float, default=0.8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = collect(args.dsn)
    verdict = assess(data, args.min_days, args.min_checks, args.max_rate, args.min_coverage)

    if args.json:
        import json
        print(json.dumps({"data": data, "verdict": verdict}, ensure_ascii=False, indent=2, default=str))
        return 0 if verdict["ready"] else 1

    print("闸门就绪度检查（能否切 ENGINEERING_GATES_MODE=block）\n")
    for gate in verdict["gates"]:
        print(f"  {'✅' if gate['ok'] else '❌'} {gate['name']} — {gate['detail']}")
    print()
    if verdict["ready"]:
        print("✅ 三项门槛全绿：可以切 block（建议先在一个产品线切，观察一周再全量）")
    else:
        missing = [g["name"] for g in verdict["gates"] if not g["ok"]]
        print("⏸️ 还不建议切 block，仍缺：")
        for name in missing:
            print(f"  - {name}")
        print("\n提示：先按计划 §7 用 warn 模式跑真实需求；误拦反馈由闸门处理人在补齐缺口时顺手标注。")
    return 0 if verdict["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
