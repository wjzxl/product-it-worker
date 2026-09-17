"""运行归档（WP7）：每次 Agent 运行留下"做了什么、花了多少、结果如何"。

用途（计划 §8 度量 + 课程图 49「晚上留证」）：
- 岗位连续运行的可观测性：成功/失败/等待、耗时、token 与成本；
- 成本控制：单岗位预算与异常消耗可追溯；
- 复盘与决策分发：一次决定影响了哪些运行。

存储：SQLAlchemy（默认 sqlite 便于本地/演练，PG 通过 DATABASE_URL 切换）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _default_url() -> str:
    env = os.getenv("RUNS_DB_URL") or os.getenv("DATABASE_URL")
    if env:
        return env
    # 本地默认落到 sqlite 文件（不污染 PG；生产用 DATABASE_URL 指向 PG）
    path = Path(__file__).resolve().parents[2] / "agent_runs.db"
    return f"sqlite:///{path}"


class AgentRun(Base):
    """一次运行（一个 thread）的归档记录。"""

    __tablename__ = "agent_runs"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sop: Mapped[str] = mapped_column(String(128), default="", index=True)
    role: Mapped[str] = mapped_column(String(64), default="", index=True)      # WP6 岗位
    trigger: Mapped[str] = mapped_column(String(32), default="manual")         # manual/schedule/event/alert
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    task_ref: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)  # running/waiting/done/failed
    checkpoint: Mapped[str] = mapped_column(String(16), default="memory")      # 状态存哪里（如实标注）
    waits: Mapped[int] = mapped_column(Integer, default=0)                     # 等待次数（数据未就绪等）
    waiting_reason: Mapped[str] = mapped_column(Text, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)                       # 网关计量；None 表示未计量
    cost_metered: Mapped[bool] = mapped_column(default=True)                   # False=unmetered（不编造）
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[float] = mapped_column(default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


_engine = None


def engine():
    global _engine
    if _engine is None:
        url = _default_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        Base.metadata.create_all(bind=_engine)
        logger.info("运行归档库就绪：%s", url.split("@")[-1])
    return _engine


def reset_engine(url: str | None = None) -> None:
    """测试/演练用：切换归档库。"""
    global _engine
    if url:
        os.environ["RUNS_DB_URL"] = url
    _engine = None
    engine()


def start_run(thread_id: str, sop: str, trigger: str = "manual", tenant: str = "default",
              task_ref: str = "", role: str = "", checkpoint: str = "memory") -> dict:
    with Session(engine()) as db:
        row = db.get(AgentRun, thread_id)
        if row is None:
            row = AgentRun(thread_id=thread_id, sop=sop, trigger=trigger, tenant=tenant,
                           task_ref=task_ref, role=role, checkpoint=checkpoint, status="running")
            db.add(row)
        else:  # 重复启动同一 thread（如恢复后重跑）只更新状态
            row.status = "running"
        db.commit()
        return _to_dict(row)


def finish_run(thread_id: str, status: str, usage: dict | None = None, detail: dict | None = None) -> dict:
    usage = usage or {}
    with Session(engine()) as db:
        row = db.get(AgentRun, thread_id)
        if row is None:
            row = AgentRun(thread_id=thread_id, status=status)
            db.add(row)
        row.status = status
        row.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        row.completion_tokens = int(usage.get("completion_tokens") or 0)
        row.cost_usd = float(usage.get("cost_usd") or 0.0)
        # 缺省 False：没有用量信息时**不得**声称"已计量 $0"（桩 LLM/未接计量都属此类）
        row.cost_metered = bool(usage.get("cost_metered", False))
        row.llm_calls = int(usage.get("llm_calls") or 0)
        row.finished_at = datetime.now(timezone.utc)
        started = row.started_at
        if started is not None:
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            row.duration_s = round((row.finished_at - started).total_seconds(), 3)
        if detail:
            row.detail = {**(row.detail or {}), **detail}
        db.commit()
        return _to_dict(row)


def mark_waiting(thread_id: str, reason: str) -> dict:
    """记录一次"等待"（对应课程图 48：数据没到就等，到齐再继续）。"""
    with Session(engine()) as db:
        row = db.get(AgentRun, thread_id)
        if row is None:
            row = AgentRun(thread_id=thread_id, status="waiting")
            db.add(row)
        row.status = "waiting"
        row.waits = (row.waits or 0) + 1
        row.waiting_reason = reason[:500]
        db.commit()
        return _to_dict(row)


def get_run(thread_id: str) -> dict | None:
    with Session(engine()) as db:
        row = db.get(AgentRun, thread_id)
        return _to_dict(row) if row else None


def list_runs(tenant: str = "", role: str = "", status: str = "", limit: int = 50) -> list[dict]:
    stmt = select(AgentRun)
    if tenant:
        stmt = stmt.where(AgentRun.tenant == tenant)
    if role:
        stmt = stmt.where(AgentRun.role == role)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    stmt = stmt.order_by(AgentRun.started_at.desc()).limit(limit)
    with Session(engine()) as db:
        return [_to_dict(r) for r in db.scalars(stmt)]


def stats(tenant: str = "") -> dict:
    """岗位运行汇总（计划 §8：无人值守成功率、人工介入次数、成本）。"""
    rows = list_runs(tenant=tenant, limit=1000)
    total = len(rows)
    by_status: dict[str, int] = {}
    cost = 0.0
    unmetered = 0
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        if row["cost_metered"]:
            cost += row["cost_usd"] or 0.0
        else:
            unmetered += 1
    finished = by_status.get("done", 0) + by_status.get("failed", 0)
    return {
        "runs": total,
        "by_status": by_status,
        "by_role": _count_by(rows, "role"),
        "by_trigger": _count_by(rows, "trigger"),
        "success_rate": round(by_status.get("done", 0) / finished, 3) if finished else None,
        "total_cost_usd": round(cost, 6),
        "unmetered_runs": unmetered,
        "total_waits": sum(r["waits"] or 0 for r in rows),
    }


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for row in rows:
        value = row.get(key) or "-"
        out[value] = out.get(value, 0) + 1
    return out


def _to_dict(row: AgentRun) -> dict:
    return {
        "thread_id": row.thread_id,
        "sop": row.sop,
        "role": row.role,
        "trigger": row.trigger,
        "tenant": row.tenant,
        "task_ref": row.task_ref,
        "status": row.status,
        "checkpoint": row.checkpoint,
        "waits": row.waits or 0,
        "waiting_reason": row.waiting_reason or "",
        "prompt_tokens": row.prompt_tokens or 0,
        "completion_tokens": row.completion_tokens or 0,
        "cost_usd": row.cost_usd or 0.0,
        "cost_metered": bool(row.cost_metered),
        "llm_calls": row.llm_calls or 0,
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "finished_at": row.finished_at.isoformat() if row.finished_at else "",
        "duration_s": row.duration_s or 0.0,
        "detail": row.detail or {},
    }
