"""超时与升级策略：计算 deadline、判断超时、应用超时动作。

安全底线（03 §3.2）：超时默认拒绝；只有显式声明 L1 且策略为
auto_approve_l1_only 时才允许放行——Phase 0 一律默认拒绝。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import utcnow


def compute_deadline(remind_after_hours: float) -> datetime:
    return utcnow() + timedelta(hours=remind_after_hours)


def is_overdue(deadline: datetime, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return now > deadline


def apply_timeout_action(escalation_policy: dict, risk_level: str) -> str:
    """返回超时后的动作：reject 或（仅 L1）auto_approve。

    规则：任何 L3 一律 reject，无论策略声明什么。
    """
    if risk_level == "L3":
        return "reject"
    if risk_level == "L1" and escalation_policy.get("timeout_action") == "auto_approve_l1_only":
        return "approve"
    return "reject"
