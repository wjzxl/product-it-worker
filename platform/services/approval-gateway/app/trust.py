"""信任分级动态升降级（Phase 3）。

依据**历史决策指标**为每个 Gate 计算信任等级，并与风险分级（03 §2）联动：

| 等级 | 条件（滚动窗口最近 N=20 条决策） | 行为 |
|---|---|---|
| L1 全自动 | 决策数 ≥ 10 且通过率 ≥ 90% 且近 5 条无退回/否决 | Gate 自动放行（留痕 `system:trust`） |
| L2 提案+审批 | 通过率 ≥ 80%（不满足 L1） | 按抽样比例抽检（`TRUST_SAMPLE_RATIO`，默认 0.2），抽样命中才需人审 |
| L3 强制审批 | 通过率 < 80% 或存在生效中的惩罚（近期退回/否决） | 全部人工审批 |

**失败即回升（降级单向门）**：任一 reject/revise 决策写入惩罚期（`TRUST_PENALTY_HOURS`，默认 24h），
惩罚期内该 Gate 强制 L3；惩罚到期后按新指标重新计算（指标自然回升才恢复 L1/L2）。
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ApprovalRequest, Decision, TrustState, utcnow
from .sop_config import trust_setting

logger = logging.getLogger(__name__)

CLEAN_TAIL = 5


def _window() -> int:
    return int(trust_setting("window", cast=int))


def _min_samples() -> int:
    return int(trust_setting("min_samples", cast=int))


def _l1_rate() -> float:
    return trust_setting("l1_approve_rate")


def _l2_rate() -> float:
    return trust_setting("l2_approve_rate")


def _sample_ratio() -> float:
    return trust_setting("sample_ratio", env_name="TRUST_SAMPLE_RATIO")


def _penalty_hours() -> float:
    return trust_setting("penalty_hours", env_name="TRUST_PENALTY_HOURS")


def _active_penalty(db: Session, scope: str, now: datetime) -> datetime | None:
    state = db.get(TrustState, scope)
    if state and state.penalty_until:
        penalty_until = state.penalty_until
        if penalty_until.tzinfo is None:
            penalty_until = penalty_until.replace(tzinfo=timezone.utc)
        if penalty_until > now:
            return penalty_until
    return None


def apply_penalty(db: Session, scope: str, reason: str, now: datetime | None = None) -> datetime:
    """退回/否决 → 写入惩罚期（强制 L3），实现"失败即回升"。"""
    now = now or utcnow()
    until = now + timedelta(hours=_penalty_hours())
    state = db.get(TrustState, scope)
    if state is None:
        state = TrustState(scope=scope)
        db.add(state)
    state.penalty_until = until
    state.reason = reason[-200:]
    state.updated_at = now
    logger.warning("信任惩罚生效 scope=%s until=%s reason=%s", scope, until.isoformat(), reason[:80])
    return until


def compute_trust(db: Session, scope: str, now: datetime | None = None) -> dict:
    """计算某 Gate 的信任指标与等级。"""
    now = now or utcnow()
    rows = db.execute(
        select(Decision.decision, Decision.decided_at)
        .join(ApprovalRequest, ApprovalRequest.id == Decision.request_id)
        .where(ApprovalRequest.type == scope)
        .order_by(Decision.decided_at.desc())
        .limit(_window())
    ).all()
    total = len(rows)
    approves = sum(1 for d, _ in rows if d == "approve")
    revises = sum(1 for d, _ in rows if d == "revise")
    rejects = sum(1 for d, _ in rows if d == "reject")
    approve_rate = approves / total if total else 0.0
    tail = [d for d, _ in rows[:CLEAN_TAIL]]
    penalty_until = _active_penalty(db, scope, now)

    if penalty_until:
        level, reason = "L3", f"惩罚期至 {penalty_until.isoformat()}（近期退回/否决）"
    elif total >= _min_samples() and approve_rate >= _l1_rate() and not any(d in ("revise", "reject") for d in tail):
        level, reason = "L1", f"通过率 {approve_rate:.0%}（{approves}/{total}）且近 {CLEAN_TAIL} 条无退回"
    elif approve_rate >= _l2_rate():
        level, reason = "L2", f"通过率 {approve_rate:.0%}（{approves}/{total}），按 {_sample_ratio():.0%} 抽样"
    else:
        level, reason = "L3", f"通过率 {approve_rate:.0%}（{approves}/{total}）低于阈值"

    return {
        "scope": scope,
        "level": level,
        "reason": reason,
        "samples": total,
        "approve_rate": round(approve_rate, 3),
        "approves": approves,
        "revises": revises,
        "rejects": rejects,
        "sample_ratio": _sample_ratio() if level == "L2" else (0.0 if level == "L1" else 1.0),
        "penalty_until": penalty_until.isoformat() if penalty_until else None,
        "computed_at": now.isoformat(),
    }


def all_trust(db: Session, now: datetime | None = None) -> list[dict]:
    scopes = [row[0] for row in db.execute(select(ApprovalRequest.type).distinct()).all()]
    return [compute_trust(db, scope, now=now) for scope in sorted(scopes)]


def should_auto_approve(trust: dict, token: str) -> tuple[bool, str]:
    """依据信任等级决定是否自动放行。

    L1 → 直接放行；L2 → 按抽样比例（确定性哈希，可复现）抽检；
    L3 → 全部人工。返回 (是否放行, 说明)。
    """
    level = trust.get("level")
    if level == "L1":
        return True, "L1 全自动放行（信任等级达标）"
    if level == "L2":
        ratio = float(trust.get("sample_ratio") or 0.0)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        if bucket < ratio:
            return True, f"L2 抽样命中自动放行（比例 {ratio:.0%}）"
        return False, f"L2 抽样未命中，需人工审批（比例 {ratio:.0%}）"
    return False, "L3 强制人工审批"
