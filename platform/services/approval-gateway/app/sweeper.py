"""超时扫描器：到期未决审批按策略自动处置（默认拒绝，绝不静默放行）。

运行方式（Phase 0，三选一）：
- 一次性：  python -m app.sweep
- 定时循环：SWEEP_INTERVAL_SECONDS=300 uvicorn app.main:app
- 手动触发：POST /admin/sweep
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .events import new_event, publisher_from_env
from .models import ApprovalRequest, Decision, utcnow
from .policies import apply_timeout_action, is_overdue

logger = logging.getLogger(__name__)


def _publish_targets() -> list:
    raw = os.getenv("PUBLISH_TARGETS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


_publisher = publisher_from_env(_publish_targets() or None)


def sweep(db: Session) -> list[dict]:
    """扫描 pending 且已过 deadline 的审批，按策略处置并留痕。"""
    now = utcnow()
    pending = db.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending")).all()
    handled: list[dict] = []
    for req in pending:
        if not is_overdue(req.deadline, now):
            continue
        risk_level = (req.context_snapshot or {}).get("risk", {}).get("level", "L3")
        action = apply_timeout_action(req.escalation_policy or {}, risk_level)
        decision = Decision(
            request_id=req.id,
            decision=action,
            decided_by="system:timeout",
            comment=f"超时未处理，按策略自动{action}（风险等级 {risk_level}）",
        )
        db.add(decision)
        req.status = "approved" if action == "approve" else "rejected"
        handled.append({"approval_id": req.id, "action": action, "risk_level": risk_level})
        _publisher.publish(
            new_event(
                "approval.decided",
                {
                    "approval_id": req.id,
                    "task_ref": req.task_ref,
                    "decision": action,
                    "decided_by": "system:timeout",
                    "comment": decision.comment,
                    "channel": "timeout",
                    "version": req.version,
                },
                "approval-gateway",
            )
        )
        logger.warning("审批超时自动处置: %s -> %s（%s）", req.id, action, risk_level)
    if handled:
        db.commit()
    return handled


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as db:
        result = sweep(db)
        print(f"sweep done: handled={len(result)}")
        for item in result:
            print(item)
