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


def apply_timeout_action(escalation_policy: dict, trusted_level: str) -> str:
    """返回超时后的动作：reject 或（仅服务端信任等级 L1）auto_approve。

    **修复 BUG-004（CRITICAL）**：第二个参数必须是**服务端计算的信任等级**
    （`trust.compute_trust()`），绝不能是单据里客户端自述的 `risk.level`——
    否则提交方只要自封 `level=L1` 就能换取超时自动放行，人审被完全绕过。

    规则：
    - `timeout_action` 不是 `auto_approve_l1_only` → 一律 reject（默认拒绝）；
    - 即使声明了自动放行，也仅在服务端信任等级为 L1 时生效；
    - 其余（L2/L3/未知）→ reject。

    超时默认拒绝是平台的安全底线：**绝不静默放行**。
    """
    if escalation_policy.get("timeout_action") != "auto_approve_l1_only":
        return "reject"
    return "approve" if trusted_level == "L1" else "reject"
