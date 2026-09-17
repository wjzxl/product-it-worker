"""审批网关 API。

- 工作流引擎在 Gate 处调用 POST /approvals 创建审批（三要素校验）；
- 人在 Web 审批台或 IM 卡片上做决策；
- 决策结果写审计留痕，并发布 approval.decided 事件（Phase 0 事件走回调/轮询）。
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .events import new_event, publisher_from_env
from .models import ApprovalRequest, Decision, utcnow
from .notifier import get_notifier
from .policies import is_overdue
from .auth import auth_summary, current_principal
from .trust import all_trust, apply_penalty, compute_trust, should_auto_approve, tenant_summary
from .schemas import (
    ApprovalRequestCreate,
    ApprovalRequestOut,
    DecisionIn,
    DecisionOut,
    ImCardCallback,
)

logger = logging.getLogger(__name__)
router = APIRouter()

STATUS_TERMINAL = {"approved", "rejected"}


def _publish_targets() -> list:
    import os

    raw = os.getenv("PUBLISH_TARGETS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


_publisher = publisher_from_env(_publish_targets() or None)


def current_tenant(x_tenant: str = Header(default="")) -> str:
    """多租户：请求头 X-Tenant 指定租户（缺省取 DEFAULT_TENANT）。"""
    import os

    return x_tenant or os.getenv("DEFAULT_TENANT", "default")


def _get_or_404(db: Session, approval_id: str, tenant: Optional[str] = None) -> ApprovalRequest:
    req = db.get(ApprovalRequest, approval_id)
    if req is None or (tenant is not None and req.tenant != tenant):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"approval {approval_id} 不存在")
    return req


def _check_pending(req: ApprovalRequest) -> None:
    if req.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"审批已处理（当前状态 {req.status}）")
    if is_overdue(req.deadline):
        raise HTTPException(status.HTTP_410_GONE, "审批已超时，请按超时策略重新发起")


@router.post("/approvals", response_model=ApprovalRequestOut, status_code=status.HTTP_201_CREATED)
def create_approval(
    payload: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> ApprovalRequest:
    """工作流到达 Gate 时调用。三要素校验已在 schema 层强制。

    `auto_gate=True` 时按信任等级决定：L1 自动放行 / L2 抽样命中自动放行 / L3 人工审批。
    自动放行一律留痕（决策人 `system:trust`），并发布 approval.decided 事件。
    """
    req = ApprovalRequest(
        tenant=tenant,
        type=payload.type,
        stage=payload.stage,
        requester=payload.requester,
        task_ref=payload.task_ref,
        context_snapshot=payload.context_snapshot.model_dump(mode="json"),
        # WP1：来源链冗余到独立列，便于按来源/任务反查（空列表=存量数据，不算缺失）
        sources=[s.model_dump(mode="json") for s in payload.context_snapshot.sources],
        options=[o for o in payload.options],
        deadline=payload.deadline,
        escalation_policy=payload.escalation_policy.model_dump(),
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # 推送审批卡片（Web/IM 双通道；Phase 0 日志桩）
    get_notifier().send(req)
    # 发布 approval.requested 事件（契约 contracts/events.md），驱动工作流扇出
    _publisher.publish(
        new_event(
            "approval.requested",
            {
                "approval_id": req.id,
                "gate": req.type,
                "stage": req.stage,
                "task_ref": req.task_ref,
                "requester": req.requester,
                "deadline": req.deadline.isoformat(),
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    logger.info("approval.requested id=%s stage=%s", req.id, req.stage)

    trust_info = compute_trust(db, req.type, tenant=req.tenant)
    auto_decision = "pending"
    if payload.auto_gate:
        allowed, why = should_auto_approve(trust_info, req.id)
        if allowed:
            decision = Decision(
                request_id=req.id,
                decision="approve",
                decided_by="system:trust",
                comment=f"信任自动放行：{why}（{trust_info['reason']}）",
            )
            db.add(decision)
            req.status = "approved"
            db.commit()
            db.refresh(req)
            auto_decision = "approved"
            _publisher.publish(
                new_event(
                    "approval.decided",
                    {
                        "approval_id": req.id,
                        "task_ref": req.task_ref,
                        "decision": "approve",
                        "decided_by": "system:trust",
                        "comment": decision.comment,
                        "channel": "trust",
                        "version": req.version,
                    },
                    "approval-gateway",
                )
            )
            logger.info("信任自动放行 id=%s level=%s why=%s", req.id, trust_info["level"], why)
        else:
            auto_decision = "sampled_review" if trust_info["level"] == "L2" else "pending"

    out = ApprovalRequestOut.model_validate(req)
    out.trust_level = trust_info["level"]
    out.trust_reason = trust_info["reason"]
    out.auto_decision = auto_decision
    return out


@router.get("/approvals", response_model=list[ApprovalRequestOut])
def list_approvals(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant == tenant).order_by(ApprovalRequest.created_at.desc())
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    return list(db.scalars(stmt))


@router.get("/approvals/{approval_id}", response_model=ApprovalRequestOut)
def get_approval(approval_id: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> ApprovalRequest:
    return _get_or_404(db, approval_id, tenant=tenant)


@router.get("/requirements/{task_ref}/provenance")
def requirement_provenance(
    task_ref: str,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> dict:
    """需求来源链反查（WP1）：产物/task_ref → 当初是谁、在哪句话/哪个单元格里提出的。

    对应课程图 11「每条需求都带出处」与图 33「证据要能复验」：
    争论某个字段/口径为什么存在时，从这里回查，而不是翻聊天记录。

    返回按 Gate 时间线聚合的来源条目（含未确认的候选），并给出**仍缺出处的 Gate**，
    便于工程闸门 E1 直接消费。
    """
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.task_ref == task_ref, ApprovalRequest.tenant == tenant)
        .order_by(ApprovalRequest.created_at.asc())
    )
    rows = list(db.scalars(stmt))
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"任务 {task_ref} 在本产品线无审批记录")

    entries: list[dict] = []
    seen: set[tuple] = set()
    gates_without_sources: list[str] = []
    for req in rows:
        stored = req.sources or (req.context_snapshot or {}).get("sources") or []
        if not stored:
            gates_without_sources.append(req.type)
            continue
        for item in stored:
            key = (item.get("kind"), item.get("ref"), json.dumps(item.get("locator") or {}, sort_keys=True))
            if key in seen:      # 同一出处被多个 Gate 引用时只记一次，保留首次确认时间
                continue
            seen.add(key)
            entries.append({**item, "gate": req.type, "approval_id": req.id, "recorded_at": req.created_at.isoformat()})

    by_kind: dict[str, int] = {}
    for item in entries:
        by_kind[item.get("kind", "unknown")] = by_kind.get(item.get("kind", "unknown"), 0) + 1

    return {
        "task_ref": task_ref,
        "tenant": tenant,
        "gates": [r.type for r in rows],
        "sources": entries,
        "counts_by_kind": by_kind,
        "gates_without_sources": sorted(set(gates_without_sources)),
        "has_provenance": bool(entries),
    }


@router.post("/approvals/{approval_id}/decision", response_model=DecisionOut)
def decide(
    approval_id: str,
    payload: DecisionIn,
    db: Session = Depends(get_db),
    principal=Depends(current_principal),
    tenant: str = Depends(current_tenant),
) -> Decision:
    """人做决策（Web 审批台入口）。AUTH_MODE=oidc/static 时要求登录（主体写入审计）。"""
    req = _get_or_404(db, approval_id, tenant=tenant)
    _check_pending(req)

    decided_by = payload.decided_by or principal.sub
    decision = Decision(
        request_id=req.id,
        decision=payload.decision,
        decided_by=decided_by,
        comment=payload.comment,
    )
    db.add(decision)

    if payload.decision == "approve":
        req.status = "approved"
    elif payload.decision == "reject":
        req.status = "rejected"
        apply_penalty(db, req.type, f"否决：{payload.comment}", tenant=req.tenant)
    else:  # revise：退回修订，允许修订后重新提交（version+1）
        req.status = "revised"
        apply_penalty(db, req.type, f"退回：{payload.comment}", tenant=req.tenant)

    db.commit()
    db.refresh(decision)
    # 发布 approval.decided 事件（契约 contracts/events.md），驱动工作流恢复/回退
    _publisher.publish(
        new_event(
            "approval.decided",
            {
                "approval_id": req.id,
                "task_ref": req.task_ref,
                "decision": decision.decision,
                "decided_by": decision.decided_by,
                "comment": decision.comment,
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    logger.info(
        "approval.decided id=%s decision=%s by=%s comment=%s",
        req.id, decision.decision, decision.decided_by, decision.comment,
    )
    return decision


@router.post("/approvals/{approval_id}/resubmit", response_model=ApprovalRequestOut)
def resubmit(
    approval_id: str,
    payload: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    principal=Depends(current_principal),
) -> ApprovalRequest:
    """revise 后修订重新提交：新上下文（version+1），重新进入 pending。"""
    req = _get_or_404(db, approval_id)
    if req.status != "revised":
        raise HTTPException(status.HTTP_409_CONFLICT, "仅 revised 状态的审批可重新提交")

    req.context_snapshot = payload.context_snapshot.model_dump()
    req.deadline = payload.deadline
    req.escalation_policy = payload.escalation_policy.model_dump()
    req.status = "pending"
    req.version += 1
    db.commit()
    db.refresh(req)
    get_notifier().send(req)
    _publisher.publish(
        new_event(
            "approval.requested",
            {
                "approval_id": req.id,
                "gate": req.type,
                "stage": req.stage,
                "task_ref": req.task_ref,
                "requester": req.requester,
                "deadline": req.deadline.isoformat(),
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    return req


@router.post("/callbacks/im/card", response_model=DecisionOut)
async def im_card_callback(payload: ImCardCallback, request: Request, db: Session = Depends(get_db)) -> Decision:
    """IM 交互卡片回调：卡片按钮 → 决策。"""
    # TODO(T0.4): 飞书签名校验（timestamp+sign），防伪造回调
    req = _get_or_404(db, payload.approval_id)
    _check_pending(req)

    decision = Decision(
        request_id=req.id,
        decision=payload.action,
        decided_by=payload.operator,
        comment=payload.comment,
    )
    db.add(decision)
    if payload.action == "approve":
        req.status = "approved"
    elif payload.action == "reject":
        req.status = "rejected"
        apply_penalty(db, req.type, f"否决(IM)：{payload.comment}", tenant=req.tenant)
    else:
        req.status = "revised"
        apply_penalty(db, req.type, f"退回(IM)：{payload.comment}", tenant=req.tenant)
    db.commit()
    db.refresh(decision)
    _publisher.publish(
        new_event(
            "approval.decided",
            {
                "approval_id": req.id,
                "task_ref": req.task_ref,
                "decision": decision.decision,
                "decided_by": decision.decided_by,
                "comment": decision.comment,
                "channel": "im",
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    logger.info(
        "approval.decided(im) id=%s decision=%s by=%s", req.id, decision.decision, decision.decided_by
    )
    return decision


@router.get("/auth/info")
def auth_info(principal=Depends(current_principal)) -> dict:
    """当前认证配置与主体（前端可据此决定是否跳转 SSO 登录）。"""
    return {**auth_summary(), "principal": {"sub": principal.sub, "roles": principal.roles}}


@router.get("/trust")
def trust_all(db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> list[dict]:
    """本产品线（X-Tenant）各 Gate 的信任等级与指标（Phase 3 动态升降级）。

    注意：信任按 **产品线 × Gate** 隔离——新产品线从零积累，不继承其它产品线的 L1。
    """
    return all_trust(db, tenant=tenant)


@router.get("/trust/overview")
def trust_overview(db: Session = Depends(get_db)) -> list[dict]:
    """平台管理员总览：各产品线的 Gate 数、等级分布、自动放行占比（多产品线推广看板）。"""
    return tenant_summary(db)


@router.get("/trust/{gate_type}")
def trust_one(gate_type: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> dict:
    return compute_trust(db, gate_type, tenant=tenant)


@router.post("/admin/sweep")
def admin_sweep(db: Session = Depends(get_db), principal=Depends(current_principal)) -> dict:
    """手动触发超时扫描（等价于 python -m app.sweep）。"""
    from .sweeper import sweep

    handled = sweep(db)
    return {"handled": len(handled), "items": handled}
