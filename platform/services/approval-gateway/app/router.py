"""审批网关 API。

- 工作流引擎在 Gate 处调用 POST /approvals 创建审批（三要素校验）；
- 人在 Web 审批台或 IM 卡片上做决策；
- 决策结果写审计留痕，并发布 approval.decided 事件（Phase 0 事件走回调/轮询）。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .events import new_event, publisher_from_env
from .models import ApprovalRequest, Decision, utcnow
from .notifier import get_notifier
from .policies import is_overdue
from .trust import all_trust, apply_penalty, compute_trust, should_auto_approve
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


def _get_or_404(db: Session, approval_id: str) -> ApprovalRequest:
    req = db.get(ApprovalRequest, approval_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"approval {approval_id} 不存在")
    return req


def _check_pending(req: ApprovalRequest) -> None:
    if req.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"审批已处理（当前状态 {req.status}）")
    if is_overdue(req.deadline):
        raise HTTPException(status.HTTP_410_GONE, "审批已超时，请按超时策略重新发起")


@router.post("/approvals", response_model=ApprovalRequestOut, status_code=status.HTTP_201_CREATED)
def create_approval(payload: ApprovalRequestCreate, db: Session = Depends(get_db)) -> ApprovalRequest:
    """工作流到达 Gate 时调用。三要素校验已在 schema 层强制。

    `auto_gate=True` 时按信任等级决定：L1 自动放行 / L2 抽样命中自动放行 / L3 人工审批。
    自动放行一律留痕（决策人 `system:trust`），并发布 approval.decided 事件。
    """
    req = ApprovalRequest(
        type=payload.type,
        stage=payload.stage,
        requester=payload.requester,
        task_ref=payload.task_ref,
        context_snapshot=payload.context_snapshot.model_dump(),
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

    trust_info = compute_trust(db, req.type)
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
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    return list(db.scalars(stmt))


@router.get("/approvals/{approval_id}", response_model=ApprovalRequestOut)
def get_approval(approval_id: str, db: Session = Depends(get_db)) -> ApprovalRequest:
    return _get_or_404(db, approval_id)


@router.post("/approvals/{approval_id}/decision", response_model=DecisionOut)
def decide(
    approval_id: str,
    payload: DecisionIn,
    db: Session = Depends(get_db),
) -> Decision:
    """人做决策（Web 审批台入口）。"""
    req = _get_or_404(db, approval_id)
    _check_pending(req)

    decision = Decision(
        request_id=req.id,
        decision=payload.decision,
        decided_by=payload.decided_by,
        comment=payload.comment,
    )
    db.add(decision)

    if payload.decision == "approve":
        req.status = "approved"
    elif payload.decision == "reject":
        req.status = "rejected"
        apply_penalty(db, req.type, f"否决：{payload.comment}")
    else:  # revise：退回修订，允许修订后重新提交（version+1）
        req.status = "revised"
        apply_penalty(db, req.type, f"退回：{payload.comment}")

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
        apply_penalty(db, req.type, f"否决(IM)：{payload.comment}")
    else:
        req.status = "revised"
        apply_penalty(db, req.type, f"退回(IM)：{payload.comment}")
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


@router.get("/trust")
def trust_all(db: Session = Depends(get_db)) -> list[dict]:
    """全部 Gate 的信任等级与指标（Phase 3 动态升降级）。"""
    return all_trust(db)


@router.get("/trust/{gate_type}")
def trust_one(gate_type: str, db: Session = Depends(get_db)) -> dict:
    return compute_trust(db, gate_type)


@router.post("/admin/sweep")
def admin_sweep(db: Session = Depends(get_db)) -> dict:
    """手动触发超时扫描（等价于 python -m app.sweep）。"""
    from .sweeper import sweep

    handled = sweep(db)
    return {"handled": len(handled), "items": handled}
