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
from .trust import compute_trust

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
        # 修复 BUG-004：等级取自**服务端信任计算**，不读单据自述的 risk.level
        trust = compute_trust(db, req.type, tenant=req.tenant, now=now)
        trusted_level = trust["level"]
        action = apply_timeout_action(req.escalation_policy or {}, trusted_level)
        declared = (req.context_snapshot or {}).get("risk", {}).get("level", "?")
        decision = Decision(
            request_id=req.id,
            decision=action,
            decided_by="system:timeout",
            comment=(
                f"超时未处理，按策略自动{action}"
                f"（服务端信任等级 {trusted_level}；单据自述等级 {declared} 不作为依据）"
            ),
        )
        db.add(decision)
        req.status = "approved" if action == "approve" else "rejected"
        handled.append({"approval_id": req.id, "action": action,
                        "trusted_level": trusted_level, "declared_level": declared})
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
        logger.warning("审批超时自动处置: %s -> %s（服务端信任=%s，单据自述=%s）",
                       req.id, action, trusted_level, declared)
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
