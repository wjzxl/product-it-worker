#!/usr/bin/env python3
"""三域指标汇总：从 PostgreSQL（平台库 + LiteLLM 库）统计 Gate 决策、任务、成本指标。

用法（需 approval-gateway 的 venv 提供 psycopg）：
  services/approval-gateway/.venv/bin/python scripts/metrics_report.py
输出：控制台表格 + platform/reports/phase2-metrics.md
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATFORM_DSN = os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform")
LITELLM_DSN = os.getenv("LITELLM_DSN", "host=127.0.0.1 port=5432 dbname=litellm user=platform password=platform")

GATE_LABEL = {
    "GATE_REQUIREMENT_REVIEW": "G1 需求评审",
    "GATE_TECH_DESIGN_REVIEW": "G2 方案评审",
    "GATE_PR_REVIEW": "G3 代码审查",
    "GATE_RELEASE_DECISION": "G4 放行决策",
    "GATE_CHANGE_APPROVAL": "G5 变更审批",
    "GATE_POSTMORTEM_REVIEW": "G6 复盘审查",
    "GATE_PROCESS_IMPROVEMENT": "G7 流程改进",  # 平台域：SOP 自进化提案闸门（Phase 3）
}
SOP_LABEL = {
    "product-domain/prd-flow": "产品域",
    "dev-domain/issue-to-merge": "研发域",
    "test-domain/release-gate": "测试域",
    "ops-domain/alert-to-postmortem": "运维域",
}


def collect() -> dict:
    data: dict = {}
    with psycopg.connect(PLATFORM_DSN) as conn:
        # 审批与决策
        data["approvals_total"] = conn.execute("select count(*) from approval_requests").fetchone()[0]
        data["by_gate"] = conn.execute(
            "select type, count(*), avg(extract(epoch from (d.decided_at - a.created_at))) "
            "from approval_requests a left join approval_decisions d on d.request_id = a.id "
            "group by type order by type"
        ).fetchall()
        data["status_dist"] = conn.execute(
            "select status, count(*) from approval_requests group by status order by 2 desc"
        ).fetchall()
        data["decision_dist"] = conn.execute(
            "select decision, count(*) from approval_decisions group by decision order by 2 desc"
        ).fetchall()
        data["decision_avg"] = conn.execute(
            "select avg(extract(epoch from (d.decided_at - a.created_at))) "
            "from approval_decisions d join approval_requests a on a.id = d.request_id"
        ).fetchone()[0]
        # 任务
        data["tasks_total"] = conn.execute("select count(*) from tasks").fetchone()[0]
        data["tasks_by_sop"] = conn.execute(
            "select sop, count(*), count(*) filter (where status = 'done') from tasks group by sop order by 2 desc"
        ).fetchall()
        data["tasks_by_status"] = conn.execute(
            "select status, count(*) from tasks group by status order by 2 desc"
        ).fetchall()
        data["events_total"] = conn.execute("select count(*) from task_events").fetchone()[0]
    try:
        with psycopg.connect(LITELLM_DSN) as conn:
            data["spend_total"] = conn.execute('select coalesce(sum(spend), 0) from "LiteLLM_SpendLogs"').fetchone()[0]
            data["spend_by_model"] = conn.execute(
                'select model, count(*), coalesce(sum(spend), 0), coalesce(sum("total_tokens"), 0) '
                'from "LiteLLM_SpendLogs" group by model order by 3 desc limit 8'
            ).fetchall()
    except psycopg.Error as exc:
        data["spend_total"] = None
        data["spend_by_model"] = []
        print(f"（LiteLLM 库读取失败：{exc}）", file=sys.stderr)
    return data


def render(data: dict) -> list[str]:
    lines = [
        f"# Phase 2 三域指标汇总（{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}）",
        "",
        f"- 审批单总数：**{data['approvals_total']}**｜任务总数：**{data['tasks_total']}**｜时间线事件：**{data['events_total']}**",
        f"- 人审决策平均耗时：**{data['decision_avg']:.1f}s**" if data["decision_avg"] else "- 人审决策平均耗时：-",
        "",
        "## Gate 决策（按 Gate 类型）",
        "",
        "| Gate | 审批数 | 平均决策耗时(s) |",
        "|---|---|---|",
    ]
    for gtype, cnt, avg in data["by_gate"]:
        lines.append(f"| {GATE_LABEL.get(gtype, gtype)} | {cnt} | {f'{avg:.1f}' if avg else '-'} |")
    lines += ["", "## 任务（按域）", "", "| 域 | 任务数 | 完成 |", "|---|---|---|"]
    for sop, cnt, done in data["tasks_by_sop"]:
        lines.append(f"| {SOP_LABEL.get(sop, sop or '-')} | {cnt} | {done} |")
    lines += ["", "## 审批状态分布", "", "| 状态 | 数量 |", "|---|---|"]
    for st, cnt in data["status_dist"]:
        lines.append(f"| {st} | {cnt} |")
    lines += ["", "## 决策分布", "", "| 决策 | 数量 |", "|---|---|"]
    for d, cnt in data["decision_dist"]:
        lines.append(f"| {d} | {cnt} |")
    if data.get("spend_total") is not None:
        lines += ["", "## LLM 成本（LiteLLM 计量）", "", f"累计花费：**${data['spend_total']:.6f}**", "",
                  "| 模型 | 调用数 | 花费($) | tokens |", "|---|---|---|---|"]
        for model, cnt, spend, tokens in data["spend_by_model"]:
            lines.append(f"| {model} | {cnt} | {spend:.6f} | {tokens} |")
    return lines


def main() -> int:
    data = collect()
    lines = render(data)
    print("\n".join(lines))
    out_dir = os.path.join(ROOT, "reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "phase2-metrics.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
