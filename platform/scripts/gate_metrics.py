#!/usr/bin/env python3
"""工程闸门度量（计划 §8）：拦截率、缺口分布、缺口补齐时长、证据覆盖。

数据源：approval-gateway 的 `gate_checks` / `evidence_artifacts`（真实留痕，非估算）。

设计要点：
- **不编造指标**：样本为 0 时直接报 0 并说明"尚无真实样本"，不拿演练数据充当生产数据；
- `--source` 可指向任意 DSN（默认平台库），便于对齐演练库与生产库；
- 误拦率需要**人工反馈**（人判断"其实可放行"），当前表里没有该字段 →
  脚本如实标注"需人工标注"，并给出标注入口建议（计划 §8 的误拦率是决策依据）。

用法：
    cd platform
    services/metrics-dashboard/.venv/bin/python scripts/gate_metrics.py
    ... --dsn "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform"
    ... --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
# 用 SQLAlchemy URL：既能连 PG（postgresql+psycopg://...），也能直接指向演练用的 sqlite
DEFAULT_URL = os.getenv(
    "PLATFORM_DB_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform"),
)


def _as_dt(value):
    """时间列在原生 SQL 下可能返回字符串（sqlite），统一成 datetime。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _as_list(value) -> list:
    """JSON 列在原生 SQL 下可能返回字符串（取决于驱动），统一解析成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


class MetricsUnavailable(RuntimeError):
    """无法度量（连接失败或表不存在）——必须与"确实没有数据"区分开。"""


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    return conn.execute(text(sql), params).fetchall()   # 错误向上抛，由 collect 区分处理


def collect(dsn: str) -> dict:
    """采集闸门度量。表不存在时返回空样本（而非报错），便于新旧库通用。"""
    out: dict = {"has_data": False, "checks_total": 0}
    engine = create_engine(dsn, future=True)
    try:
        conn_ctx = engine.connect()
    except SQLAlchemyError as exc:
        raise MetricsUnavailable(f"数据库连接失败：{type(exc).__name__}: {str(exc)[:200]}") from exc
    with conn_ctx as conn:
        try:
            rows = _rows(
                conn,
                "select code, mode, passed, blocked, tenant, task_ref, gaps, rule_version, checked_at "
                "from gate_checks order by checked_at asc",
            )
        except SQLAlchemyError as exc:
            # 表不存在 = 服务尚未用新代码对该库启动过（不是"没有拦截记录"）
            raise MetricsUnavailable(
                f"gate_checks 表不存在或不可读（{type(exc).__name__}）："
                f"请先用 approval-gateway 连接该库启动一次以完成建表迁移"
            ) from exc
        out["checks_total"] = len(rows)
        if not rows:
            out.update({"by_code": {}, "blocked_by_code": {}, "gap_counter": {}, "fill_times": [],
                        "evidence_total": 0, "evidence_by_task": {}})
            ev = _rows(conn, "select count(*) from evidence_artifacts")
            out["evidence_total"] = ev[0][0] if ev else 0
            return out

        by_code: dict[str, dict] = defaultdict(lambda: {"checks": 0, "blocked": 0, "passed": 0, "modes": Counter()})
        gap_counter: Counter = Counter()
        # 缺口补齐时长：同一 (tenant, task_ref, code) 从首次 blocked 到首次 passed 的间隔
        first_blocked: dict[tuple, object] = {}
        fill_times: list[float] = []
        for code, mode, passed, blocked, tenant, task_ref, gaps, rule_version, checked_at in rows:
            entry = by_code[code]
            entry["checks"] += 1
            entry["modes"][mode] += 1
            if passed:
                entry["passed"] += 1
            if blocked:
                entry["blocked"] += 1
            for gap in _as_list(gaps):
                gap_id = gap.get("id", "unknown") if isinstance(gap, dict) else str(gap)
                gap_counter[gap_id] += 1
            key = (tenant, task_ref, code)
            ts = _as_dt(checked_at)
            if ts is None:
                continue
            if blocked and key not in first_blocked:
                first_blocked[key] = ts
            if passed and key in first_blocked:
                delta = (ts - first_blocked.pop(key)).total_seconds()
                if delta >= 0:
                    fill_times.append(delta)

        out["by_code"] = {
            code: {
                "checks": e["checks"],
                "passed": e["passed"],
                "blocked": e["blocked"],
                "block_rate": round(e["blocked"] / e["checks"], 3) if e["checks"] else 0.0,
                "modes": dict(e["modes"]),
            }
            for code, e in sorted(by_code.items())
        }
        out["blocked_by_code"] = {code: e["blocked"] for code, e in out["by_code"].items()}
        out["gap_counter"] = dict(gap_counter.most_common())
        out["fill_times"] = fill_times
        out["has_data"] = True

        try:
            ev = _rows(conn, "select task_ref, count(*) from evidence_artifacts group by task_ref")
            out["evidence_total"] = sum(r[1] for r in ev) if ev else 0
            out["evidence_by_task"] = {r[0]: r[1] for r in ev} if ev else {}
        except SQLAlchemyError:
            out["evidence_total"] = None
            out["evidence_by_task"] = {}
    return out


def render(data: dict) -> str:
    lines = ["# 工程闸门度量（计划 §8）", ""]
    if not data["has_data"]:
        lines += [
            f"- `gate_checks` 中**尚无记录**（检查总数 {data['checks_total']}）",
            "- 说明：闸门已在代码路径上接线（E1 在 coder 生成前调用），但真实流水线尚未产生样本；",
            "  演练数据位于临时库，**不作为生产指标**。",
            f"- 证据库现有工件 {data['evidence_total']} 件",
            "",
            "**结论**：拦截率/误拦率/补齐时长**当前无法评估**——需先把闸门接入真实需求（建议 warn 模式灰度）。",
        ]
        return "\n".join(lines)

    lines += ["| 闸门 | 检查次数 | 通过 | 阻断 | 拦截率 | 模式分布 |", "|---|---|---|---|---|---|"]
    for code, e in data["by_code"].items():
        lines.append(f"| {code} | {e['checks']} | {e['passed']} | {e['blocked']} | {e['block_rate']:.1%} | {e['modes']} |")

    lines += ["", "## 缺口分布（哪些条件最常缺失）", "", "| 缺口 | 次数 |", "|---|---|"]
    for gap, count in data["gap_counter"].items():
        lines.append(f"| {gap} | {count} |")

    fills = data["fill_times"]
    lines += ["", "## 缺口补齐时长（首次被拦 → 补齐通过）", ""]
    if fills:
        ordered = sorted(fills)
        median = ordered[len(ordered) // 2]
        lines.append(f"- 样本 {len(fills)} 次，中位 {median:.0f}s，最长 {max(fills):.0f}s")
    else:
        lines.append("- 无配对样本（尚未出现「被拦后补齐通过」的完整链路）")

    lines += [
        "",
        "## 误拦率",
        "",
        "- ⚠️ **需要人工反馈**：当前 `gate_checks` 没有「人判断其实可放行」的字段，",
        "  故误拦率无法自动计算。计划 §8 约定误拦率 <10%，请通过驾驶舱（WP10）或整改说明补标注后再评。",
        "",
        f"- 证据库工件：{data['evidence_total']} 件，覆盖 {len(data['evidence_by_task'])} 个任务",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="工程闸门度量（只读）")
    parser.add_argument("--dsn", default=DEFAULT_URL, help="SQLAlchemy URL（PG 或 sqlite）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        data = collect(args.dsn)
    except MetricsUnavailable as exc:
        print(f"❌ 无法度量：{exc}", file=sys.stderr)
        print("   提示：本脚本只读；'表不存在' 通常是服务尚未对该库启动过（建表在服务启动时执行）。",
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
