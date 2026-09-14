"""流程挖掘：从平台真实数据（审批/时间线/成本）挖掘瓶颈与变体。

指标口径：
- **Gate 等待时长**：审批创建 → 决策完成（p50/p95），超时率（决定人 system:timeout）
- **退回热点**：按 Gate 的 revise/reject 次数与占比（返工信号）
- **流程变体**：每个任务的时间线事件序列（variant）聚类，统计常见路径与偏离路径
- **成本热点**：按模型与域的 LLM 花费
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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
    "GATE_PROCESS_IMPROVEMENT": "G7 流程改进",
}
DOMAIN_OF_GATE = {
    "GATE_REQUIREMENT_REVIEW": "产品域",
    "GATE_TECH_DESIGN_REVIEW": "研发域",
    "GATE_PR_REVIEW": "研发域",
    "GATE_RELEASE_DECISION": "测试域",
    "GATE_CHANGE_APPROVAL": "运维域",
    "GATE_POSTMORTEM_REVIEW": "运维域",
    "GATE_PROCESS_IMPROVEMENT": "平台域",
}


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def mine(dsn: str | None = None, days: int = 180) -> dict:
    """产出结构化挖掘报告（供 LLM 提案与人工分析）。"""
    dsn = dsn or PLATFORM_DSN
    since = datetime.now(timezone.utc) - timedelta(days=days)
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "window_days": days}

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            select a.type, a.status, a.created_at, a.deadline,
                   d.decision, d.decided_by, d.comment, d.decided_at
            from approval_requests a
            left join approval_decisions d on d.request_id = a.id
            where a.created_at >= %s
            """,
            (since,),
        ).fetchall()

        gates: dict[str, dict] = {}
        for gtype, status, created, deadline, decision, decided_by, comment, decided_at in rows:
            g = gates.setdefault(
                gtype,
                {
                    "gate": GATE_LABEL.get(gtype, gtype),
                    "domain": DOMAIN_OF_GATE.get(gtype, "-"),
                    "requests": 0,
                    "decisions": 0,
                    "waits": [],
                    "revises": 0,
                    "rejects": 0,
                    "timeouts": 0,
                    "reasons": [],
                },
            )
            g["requests"] += 1
            if decision:
                g["decisions"] += 1
                if decided_at and created:
                    g["waits"].append((decided_at - created).total_seconds())
                if decision == "revise":
                    g["revises"] += 1
                if decision == "reject":
                    g["rejects"] += 1
                if decided_by == "system:timeout":
                    g["timeouts"] += 1
                if decision in ("revise", "reject") and comment:
                    g["reasons"].append({"gate": g["gate"], "decision": decision, "reason": comment[:200]})

        # 任务与流程变体
        tasks = conn.execute(
            "select id, external_ref, sop, status, current_step, created_at, updated_at from tasks"
        ).fetchall()
        events: dict[str, list[str]] = {}
        for task_id, etype in conn.execute(
            "select task_id, event_type from task_events order by occurred_at"
        ).fetchall():
            events.setdefault(task_id, []).append(etype)

    variants: dict[str, int] = {}
    variant_durations: dict[str, list[float]] = {}
    for task_id, external_ref, sop, status, step, created, updated in tasks:
        seq = " → ".join(events.get(task_id, [])) or "(无时间线)"
        key = f"{sop or '-'}｜{seq}"
        variants[key] = variants.get(key, 0) + 1
        if created and updated:
            variant_durations.setdefault(key, []).append((updated - created).total_seconds())

    report["gates"] = []
    for g in gates.values():
        waits = g.pop("waits")
        reasons = g.pop("reasons")
        decisions = max(1, g["decisions"])
        report["gates"].append(
            {
                **g,
                "wait_p50_s": _percentile(waits, 50),
                "wait_p95_s": _percentile(waits, 95),
                "timeout_rate": round(g["timeouts"] / decisions, 3),
                "rework_rate": round((g["revises"] + g["rejects"]) / decisions, 3),
                "reasons": reasons[:10],
            }
        )
    report["gates"].sort(key=lambda x: (-(x["wait_p95_s"] or 0), -x["requests"]))
    report["top_rework_reasons"] = sorted(
        (r for g in report["gates"] for r in g["reasons"]), key=lambda r: r["decision"]
    )[:10]

    report["variants"] = [
        {
            "variant": key,
            "count": count,
            "avg_duration_s": round(sum(variant_durations.get(key, [0])) / max(1, len(variant_durations.get(key, [1]))), 1),
        }
        for key, count in sorted(variants.items(), key=lambda kv: -kv[1])[:8]
    ]
    report["tasks_total"] = len(tasks)
    report["tasks_by_status"] = {}
    for _, _, _, status, _, _, _ in tasks:
        report["tasks_by_status"][status] = report["tasks_by_status"].get(status, 0) + 1

    # 成本热点
    try:
        with psycopg.connect(LITELLM_DSN) as conn:
            report["cost_by_model"] = [
                {"model": m, "calls": c, "spend_usd": float(s), "tokens": t}
                for m, c, s, t in conn.execute(
                    'select model, count(*), coalesce(sum(spend),0), coalesce(sum("total_tokens"),0) '
                    'from "LiteLLM_SpendLogs" group by model order by 3 desc'
                ).fetchall()
            ]
    except psycopg.Error:
        report["cost_by_model"] = []

    report["bottlenecks"] = _detect_bottlenecks(report)
    return report


def _detect_bottlenecks(report: dict) -> list[dict]:
    """规则化瓶颈识别（作为 LLM 提案的客观输入）。"""
    findings: list[dict] = []
    for g in report.get("gates", []):
        if g["rework_rate"] >= 0.15 and g["decisions"] >= 3:
            findings.append(
                {
                    "type": "high_rework",
                    "gate": g["gate"],
                    "evidence": f"退回/否决率 {g['rework_rate']:.0%}（{g['revises']} 退回 / {g['rejects']} 否决 / {g['decisions']} 决策）",
                    "hint": "提高上游产出质量（模板/预检）或调整该 Gate 的审查要点",
                }
            )
        if (g["wait_p95_s"] or 0) > 3600:
            findings.append(
                {
                    "type": "slow_gate",
                    "gate": g["gate"],
                    "evidence": f"决策等待 p95 {g['wait_p95_s']}s（p50 {g['wait_p50_s']}s）",
                    "hint": "缩短 SLA/增加提醒，或对高频低风险项采用信任抽样",
                }
            )
        if g["timeout_rate"] > 0:
            findings.append(
                {
                    "type": "timeout",
                    "gate": g["gate"],
                    "evidence": f"超时自动处置率 {g['timeout_rate']:.0%}",
                    "hint": "SLA 过紧或审批人负载过高，考虑分流/加人/放宽 SLA",
                }
            )
    for v in report.get("variants", []):
        if v["count"] >= 3 and "approval.requested → approval.decided" not in v["variant"]:
            findings.append(
                {
                    "type": "variant_deviation",
                    "variant": v["variant"][:120],
                    "evidence": f"变体出现 {v['count']} 次（平均 {v['avg_duration_s']}s），缺少完整审批留痕",
                    "hint": "检查该路径是否绕过了 Gate",
                }
            )
    return findings[:10]
