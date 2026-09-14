"""三域指标采集（PostgreSQL：平台库 + LiteLLM 库）。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg

PLATFORM_DSN = os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform")
LITELLM_DSN = os.getenv("LITELLM_DSN", "host=127.0.0.1 port=5432 dbname=litellm user=platform password=platform")

GATE_LABEL = {
    "GATE_REQUIREMENT_REVIEW": "G1 需求评审",
    "GATE_TECH_DESIGN_REVIEW": "G2 方案评审",
    "GATE_PR_REVIEW": "G3 代码审查",
    "GATE_RELEASE_DECISION": "G4 放行决策",
    "GATE_CHANGE_APPROVAL": "G5 变更审批",
    "GATE_POSTMORTEM_REVIEW": "G6 复盘审查",
}
DOMAIN_OF_GATE = {
    "GATE_REQUIREMENT_REVIEW": "产品域",
    "GATE_TECH_DESIGN_REVIEW": "研发域",
    "GATE_PR_REVIEW": "研发域",
    "GATE_RELEASE_DECISION": "测试域",
    "GATE_CHANGE_APPROVAL": "运维域",
    "GATE_POSTMORTEM_REVIEW": "运维域",
}
SOP_LABEL = {
    "product-domain/prd-flow": "产品域",
    "dev-domain/issue-to-merge": "研发域",
    "test-domain/release-gate": "测试域",
    "ops-domain/alert-to-postmortem": "运维域",
}


def collect() -> dict:
    data: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}
    with psycopg.connect(PLATFORM_DSN) as conn:
        data["approvals_total"] = conn.execute("select count(*) from approval_requests").fetchone()[0]
        gates = conn.execute(
            "select a.type, count(*), avg(extract(epoch from (d.decided_at - a.created_at))) "
            "from approval_requests a left join approval_decisions d on d.request_id = a.id "
            "group by a.type"
        ).fetchall()
        data["gates"] = [
            {
                "gate": GATE_LABEL.get(g, g),
                "domain": DOMAIN_OF_GATE.get(g, "-"),
                "count": c,
                "avg_decision_s": round(float(avg), 2) if avg is not None else None,
            }
            for g, c, avg in sorted(gates, key=lambda r: GATE_LABEL.get(r[0], r[0]))
        ]
        avg_row = conn.execute(
            "select avg(extract(epoch from (d.decided_at - a.created_at))) "
            "from approval_decisions d join approval_requests a on a.id = d.request_id"
        ).fetchone()[0]
        data["decision_avg_s"] = float(avg_row) if avg_row is not None else None
        data["status_dist"] = dict(
            conn.execute("select status, count(*) from approval_requests group by status").fetchall()
        )
        data["decision_dist"] = dict(
            conn.execute("select decision, count(*) from approval_decisions group by decision").fetchall()
        )
        data["tasks_total"] = conn.execute("select count(*) from tasks").fetchone()[0]
        data["tasks_by_sop"] = [
            {"domain": SOP_LABEL.get(sop, sop or "-"), "sop": sop, "count": c, "done": d}
            for sop, c, d in conn.execute(
                "select sop, count(*), count(*) filter (where status='done') from tasks group by sop order by 2 desc"
            ).fetchall()
        ]
        data["events_total"] = conn.execute("select count(*) from task_events").fetchone()[0]
    try:
        with psycopg.connect(LITELLM_DSN) as conn:
            data["spend_total_usd"] = float(
                conn.execute('select coalesce(sum(spend),0) from "LiteLLM_SpendLogs"').fetchone()[0] or 0
            )
            data["spend_by_model"] = [
                {"model": m, "calls": c, "spend_usd": float(s), "tokens": t}
                for m, c, s, t in conn.execute(
                    'select model, count(*), coalesce(sum(spend),0), coalesce(sum("total_tokens"),0) '
                    'from "LiteLLM_SpendLogs" group by model order by 3 desc'
                ).fetchall()
            ]
    except psycopg.Error:
        data["spend_total_usd"] = None
        data["spend_by_model"] = []
    return data
