"""任务中心领域模型：Task（任务）+ TaskEvent（时间线）+ EventInbox（幂等收件箱）。"""
import uuid
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Task(Base):
    """一条生产线任务。状态机见 state.py，对齐设计文档 02 §0。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tsk"))
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)  # 多租户隔离键
    # NULL = 无外部引用（唯一索引允许多个 NULL）；空串会被索引视为同一个值，故不再用 ""（修复 BUG-015）
    # 唯一性范围是 **(tenant, external_ref)** 而不是全局：不同产品线完全可以引用各自系统的同名单号，
    # 全局唯一会让第二个租户建单直接 409（多租户语义错误）。
    external_ref: Mapped[Optional[str]] = mapped_column(String(128), index=True,
                                                        nullable=True, default=None)
    sop: Mapped[str] = mapped_column(String(128), default="")          # 如 product-domain/prd-flow
    params: Mapped[dict] = mapped_column(JSON, default=dict)           # 流程入参（需求文本等）
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    current_step: Mapped[str] = mapped_column(String(64), default="")
    agent: Mapped[str] = mapped_column(String(64), default="")         # 当前/最后执行 Agent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", order_by="TaskEvent.occurred_at", cascade="all, delete-orphan"
    )


    __table_args__ = (
        UniqueConstraint("tenant", "external_ref", name="uq_tasks_tenant_external_ref"),
    )


class TaskEvent(Base):
    """任务时间线事件：谁在何时做了什么（双轨审计的 AI 动作轨）。"""

    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tev"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))                # status_changed / step_finished / approval_* ...
    by: Mapped[str] = mapped_column(String(128), default="")           # Agent 或人
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped["Task"] = relationship(back_populates="events")


class EventInbox(Base):
    """平台事件幂等收件箱：以信封 event_id 为主键，重复投递直接跳过。"""

    __tablename__ = "event_inbox"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
