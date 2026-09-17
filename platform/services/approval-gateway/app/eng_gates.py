"""工程闸门服务端（WP2）：判定 + 留痕 + 阻断。

落点说明（见 17 号文档 §2 架构选型）：E 闸门放在 approval-gateway **内部模块**，不新起服务——
agent-runtime 已有 `APPROVAL_GATEWAY_URL` 客户端（零新接线），且可复用本服务的 DB、租户隔离、
事件发布与审计留痕；"所有闸门在一处"也便于统一治理。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import gate_rules
from .evidence import evidence_map
from .models import GateCheck

logger = logging.getLogger(__name__)

KNOWN_CODES = ("E1", "E2", "E3")


def list_rules() -> list[dict]:
    """列出全部已定义规则（含指纹，便于确认"判定用的是哪一版"）。"""
    out = []
    for code in KNOWN_CODES:
        try:
            out.append(gate_rules.describe_rule(gate_rules.load_rule(code)))
        except gate_rules.GateRuleError as exc:
            logger.warning("规则 %s 不可用：%s", code, exc)
    return out


def build_context(db: Session, task_ref: str, tenant: str, inline: Optional[dict] = None) -> dict:
    """组装被检查的上下文：调用方显式给的字段 + 从审批/来源链/闸门历史**自动补齐**的部分。

    自动补齐的价值：闸门不必依赖调用方"记得传"，来源链、依据、前序闸门结果直接从已有数据取。
    """
    from .models import ApprovalRequest

    context: dict = dict(inline or {})
    if task_ref:
        rows = list(db.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.task_ref == task_ref, ApprovalRequest.tenant == tenant)
            .order_by(ApprovalRequest.created_at.asc())
        ))
        sources: list[dict] = []
        evidence: list[str] = []
        for req in rows:
            stored = req.sources or (req.context_snapshot or {}).get("sources") or []
            sources.extend(stored)
            evidence.extend((req.context_snapshot or {}).get("evidence") or [])
            if not context.get("prd_ref") and req.type == "GATE_REQUIREMENT_REVIEW":
                context["prd_ref"] = (req.context_snapshot or {}).get("artifact_ref", "")
        context.setdefault("sources", sources)
        context.setdefault("evidence", evidence)

        # 前序工程闸门结果（E3 的 prior_gates_passed 据此判定，避免"跳着交"）
        prior: dict[str, dict] = {}
        checks = list(db.scalars(
            select(GateCheck)
            .where(GateCheck.task_ref == task_ref, GateCheck.tenant == tenant)
            .order_by(GateCheck.checked_at.asc())
        ))
        for chk in checks:
            # 同名闸门多次检查时，以最后一次为准
            prior[chk.code] = {
                "passed": chk.passed,
                "blocked": chk.blocked,
                "mode": chk.mode,
                "checked_at": chk.checked_at.isoformat(),
                "rule_version": chk.rule_version,
            }
        context.setdefault("prior_gates", prior)

        # WP5：把**证据库里的真实证据**绑定到验收项（E3 不再依赖调用方自报 evidence）
        # 规则：验收项若在证据库里已有证据，则自动补齐 evidence_ids；调用方仍可显式声明"不适用"。
        mapping = evidence_map(db, tenant, task_ref)
        items = context.get("acceptance_items")
        if isinstance(items, list) and mapping:
            patched = []
            for index, item in enumerate(items):
                item = dict(item) if isinstance(item, dict) else {"id": f"item-{index + 1}"}
                ident = str(item.get("id") or f"item-{index + 1}")
                if not item.get("evidence") and mapping.get(ident):
                    item["evidence"] = [f"evidence://{eid}" for eid in mapping[ident]]
                    item["evidence_source"] = "evidence-store"
                patched.append(item)
            context["acceptance_items"] = patched
        context["evidence_binding"] = {k: len(v) for k, v in mapping.items()}
    context.setdefault("task_ref", task_ref)
    context.setdefault("tenant", tenant)
    return context


def run_check(
    db: Session,
    code: str,
    task_ref: str = "",
    tenant: str = "default",
    context: Optional[dict] = None,
    mode: Optional[str] = None,
    checked_by: str = "",
    persist: bool = True,
) -> dict:
    """执行一次工程闸门检查并留痕。

    返回结构即契约：`{code, mode, passed, blocked, gaps[], results[], rule_version, fingerprint, check_id}`
    —— 调用方**必须**在 `blocked=true` 时停止推进（计划 WP2 验收第 ①③ 条）。
    """
    rule = gate_rules.load_rule(code)
    merged = build_context(db, task_ref, tenant, context)
    result = gate_rules.evaluate(rule, merged, mode_override=mode)
    result["check_id"] = ""
    if persist:
        row = GateCheck(
            tenant=tenant,
            code=rule.code,
            task_ref=task_ref,
            mode=result["mode"],
            passed=result["passed"],
            blocked=result["blocked"],
            gaps=gate_rules.asdict_free(result["gaps"]),
            context=gate_rules.asdict_free({k: v for k, v in merged.items() if k != "sources"}),
            rule_version=result["rule_version"] or result["fingerprint"],
            checked_by=checked_by,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        result["check_id"] = row.id
    logger.info(
        "工程闸门 %s task=%s mode=%s passed=%s blocked=%s gaps=%d",
        code, task_ref or "-", result["mode"], result["passed"], result["blocked"], len(result["gaps"]),
    )
    return result


def history(db: Session, tenant: str, task_ref: str = "", code: str = "", limit: int = 50) -> list[dict]:
    """检查历史（度量用：拦截率、缺口补齐时长、误拦反馈）。"""
    stmt = select(GateCheck).where(GateCheck.tenant == tenant)
    if task_ref:
        stmt = stmt.where(GateCheck.task_ref == task_ref)
    if code:
        stmt = stmt.where(GateCheck.code == code)
    stmt = stmt.order_by(GateCheck.checked_at.desc()).limit(limit)
    return [
        {
            "id": r.id,
            "code": r.code,
            "task_ref": r.task_ref,
            "mode": r.mode,
            "passed": r.passed,
            "blocked": r.blocked,
            "gap_ids": [g.get("id") for g in (r.gaps or [])],
            "rule_version": r.rule_version,
            "checked_by": r.checked_by,
            "checked_at": r.checked_at.isoformat(),
        }
        for r in db.scalars(stmt)
    ]
