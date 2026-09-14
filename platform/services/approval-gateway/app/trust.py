"""信任分级动态升降级（Phase 3）。

依据**历史决策指标**为每个 Gate 计算信任等级，并与风险分级（03 §2）联动：

| 等级 | 条件（滚动窗口最近 N=20 条决策） | 行为 |
|---|---|---|
| L1 全自动 | 决策数 ≥ 10 且通过率 ≥ 90% 且近 5 条无退回/否决 | Gate 自动放行（留痕 `system:trust`） |
| L2 提案+审批 | 通过率 ≥ 80%（不满足 L1） | 按抽样比例抽检（`TRUST_SAMPLE_RATIO`，默认 0.2），抽样命中才需人审 |
| L3 强制审批 | 通过率 < 80% 或存在生效中的惩罚（近期退回/否决） | 全部人工审批 |

**失败即回升（降级单向门）**：任一 reject/revise 决策写入惩罚期（`TRUST_PENALTY_HOURS`，默认 24h），
惩罚期内该 Gate 强制 L3；惩罚到期后按新指标重新计算（指标自然回升才恢复 L1/L2）。

**多产品线隔离（Phase 3 ③）**：信任的作用域是 `租户(产品线) × Gate`，即 `trust_scope(tenant, gate)`
= `"<tenant>::<GATE>"`。新产品线**从零开始积累信任**，不会继承其它产品线的 L1；任一产品线的
退回/否决惩罚也只作用于本产品线，不会把其它产品线一起打成 L3。
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ApprovalRequest, Decision, TrustState, utcnow
from .sop_config import DEFAULT_TENANT, trust_setting

logger = logging.getLogger(__name__)

CLEAN_TAIL = 5
SCOPE_SEP = "::"


def trust_scope(tenant: Optional[str], gate: str) -> str:
    """信任作用域键：租户(产品线) × Gate。"""
    return f"{tenant or DEFAULT_TENANT}{SCOPE_SEP}{gate}"


def split_scope(scope: str) -> tuple[str, str]:
    """解析作用域键为 (租户, Gate)；历史无前缀的键归入默认租户（兼容旧数据）。"""
    if SCOPE_SEP in scope:
        tenant, gate = scope.split(SCOPE_SEP, 1)
        return (tenant or DEFAULT_TENANT), gate
    return DEFAULT_TENANT, scope


def _scope_keys(tenant: str, gate: str) -> list[str]:
    """惩罚记录候选键：租户键 + （默认租户时）历史全局键。"""
    keys = [trust_scope(tenant, gate)]
    if tenant == DEFAULT_TENANT:
        keys.append(gate)
    return keys


def _window(tenant: str | None = None) -> int:
    return int(trust_setting("window", cast=int, tenant=tenant))


def _min_samples(tenant: str | None = None) -> int:
    return int(trust_setting("min_samples", cast=int, tenant=tenant))


def _l1_rate(tenant: str | None = None) -> float:
    return trust_setting("l1_approve_rate", tenant=tenant)


def _l2_rate(tenant: str | None = None) -> float:
    return trust_setting("l2_approve_rate", tenant=tenant)


def _sample_ratio(tenant: str | None = None) -> float:
    return trust_setting("sample_ratio", env_name="TRUST_SAMPLE_RATIO", tenant=tenant)


def _penalty_hours(tenant: str | None = None) -> float:
    return trust_setting("penalty_hours", env_name="TRUST_PENALTY_HOURS", tenant=tenant)


def _active_penalty(db: Session, keys: list[str], now: datetime) -> datetime | None:
    """返回候选作用域键中最晚的生效惩罚（兼容历史无租户前缀的记录）。"""
    best: datetime | None = None
    for key in keys:
        state = db.get(TrustState, key)
        if not (state and state.penalty_until):
            continue
        penalty_until = state.penalty_until
        if penalty_until.tzinfo is None:
            penalty_until = penalty_until.replace(tzinfo=timezone.utc)
        if penalty_until > now and (best is None or penalty_until > best):
            best = penalty_until
    return best


def apply_penalty(
    db: Session,
    gate: str,
    reason: str,
    tenant: str | None = None,
    now: datetime | None = None,
) -> datetime:
    """退回/否决 → 写入惩罚期（强制 L3），实现"失败即回升"。

    惩罚写入 **租户 × Gate** 作用域：只影响本产品线的该 Gate。
    """
    tenant = tenant or DEFAULT_TENANT
    now = now or utcnow()
    scope = trust_scope(tenant, gate)
    until = now + timedelta(hours=_penalty_hours(tenant))
    state = db.get(TrustState, scope)
    if state is None:
        state = TrustState(scope=scope)
        db.add(state)
    state.penalty_until = until
    state.reason = f"[{tenant}] {reason}"[-200:]
    state.updated_at = now
    logger.warning("信任惩罚生效 scope=%s until=%s reason=%s", scope, until.isoformat(), reason[:80])
    return until


def compute_trust(
    db: Session,
    gate: str,
    tenant: str | None = None,
    now: datetime | None = None,
) -> dict:
    """计算**某产品线某 Gate** 的信任指标与等级（租户隔离）。"""
    tenant = tenant or DEFAULT_TENANT
    now = now or utcnow()
    rows = db.execute(
        select(Decision.decision, Decision.decided_at)
        .join(ApprovalRequest, ApprovalRequest.id == Decision.request_id)
        .where(ApprovalRequest.type == gate, ApprovalRequest.tenant == tenant)
        .order_by(Decision.decided_at.desc())
        .limit(_window(tenant))
    ).all()
    total = len(rows)
    approves = sum(1 for d, _ in rows if d == "approve")
    revises = sum(1 for d, _ in rows if d == "revise")
    rejects = sum(1 for d, _ in rows if d == "reject")
    approve_rate = approves / total if total else 0.0
    tail = [d for d, _ in rows[:CLEAN_TAIL]]
    penalty_until = _active_penalty(db, _scope_keys(tenant, gate), now)

    if penalty_until:
        level, reason = "L3", f"惩罚期至 {penalty_until.isoformat()}（近期退回/否决）"
    elif total >= _min_samples(tenant) and approve_rate >= _l1_rate(tenant) and not any(d in ("revise", "reject") for d in tail):
        level, reason = "L1", f"通过率 {approve_rate:.0%}（{approves}/{total}）且近 {CLEAN_TAIL} 条无退回"
    elif approve_rate >= _l2_rate(tenant):
        level, reason = "L2", f"通过率 {approve_rate:.0%}（{approves}/{total}），按 {_sample_ratio(tenant):.0%} 抽样"
    else:
        level, reason = "L3", f"通过率 {approve_rate:.0%}（{approves}/{total}）低于阈值"

    return {
        "scope": trust_scope(tenant, gate),  # 兼容旧字段：现为 <tenant>::<GATE>
        "tenant": tenant,
        "gate": gate,
        "level": level,
        "reason": reason,
        "samples": total,
        "approve_rate": round(approve_rate, 3),
        "approves": approves,
        "revises": revises,
        "rejects": rejects,
        "sample_ratio": _sample_ratio(tenant) if level == "L2" else (0.0 if level == "L1" else 1.0),
        "penalty_until": penalty_until.isoformat() if penalty_until else None,
        "computed_at": now.isoformat(),
    }


def all_trust(db: Session, tenant: str | None = None, now: datetime | None = None) -> list[dict]:
    """列出信任状态。

    - `tenant` 给定 → 只返回该产品线的 Gate（默认，接口按 X-Tenant 隔离）；
    - `tenant=None` → 返回全部产品线 × Gate（平台管理员总览）。
    """
    stmt = select(ApprovalRequest.tenant, ApprovalRequest.type).distinct()
    if tenant:
        stmt = stmt.where(ApprovalRequest.tenant == tenant)
    pairs = [(row[0] or DEFAULT_TENANT, row[1]) for row in db.execute(stmt).all()]
    return [compute_trust(db, gate, tenant=tn, now=now) for tn, gate in sorted(pairs)]


def tenant_summary(db: Session, now: datetime | None = None) -> list[dict]:
    """产品线维度汇总（多产品线推广看板）：每条产品线的 Gate 数、等级分布、自动放行占比。"""
    items = all_trust(db, tenant=None, now=now)
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item["tenant"], []).append(item)
    summary = []
    for tn, gates in sorted(buckets.items()):
        levels = {"L1": 0, "L2": 0, "L3": 0}
        for g in gates:
            levels[g["level"]] = levels.get(g["level"], 0) + 1
        samples = sum(g["samples"] for g in gates)
        summary.append(
            {
                "tenant": tn,
                "gates": len(gates),
                "levels": levels,
                "samples": samples,
                "auto_ratio": round(levels["L1"] / len(gates), 3) if gates else 0.0,
                "penalized": sum(1 for g in gates if g["penalty_until"]),
            }
        )
    return summary


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
