"""审批领域模型。

状态机（v0）：
    pending ──approve──> approved
    pending ──reject───> rejected
    pending ──revise───> revised ──(修订后重新提交, version+1)──> pending
    pending ──超时─────> rejected（L3 默认拒绝，绝不静默放行）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ApprovalRequest(Base):
    """一条审批请求（对应设计文档 03 §3.1 的审批请求模型）。"""

    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("apv"))
    type: Mapped[str] = mapped_column(String(64), index=True)          # GATE_REQUIREMENT_REVIEW ...
    stage: Mapped[str] = mapped_column(String(128))                    # 如 "产品域 G1"
    requester: Mapped[str] = mapped_column(String(128))                # 如 "产品Agent@issue-1234"
    task_ref: Mapped[str] = mapped_column(String(128), default="")

    # 三要素上下文快照（创建后不可变，审批人看到一致视图）
    context_snapshot: Mapped[dict] = mapped_column(JSON)

    options: Mapped[list] = mapped_column(JSON, default=list)          # ["approve","reject","revise"]

    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    escalation_policy: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    version: Mapped[int] = mapped_column(default=1)                    # 退回修订后 +1

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="request", order_by="Decision.decided_at"
    )


class Decision(Base):
    """人审决策留痕（审计要求：谁、何时、批了什么、理由）。"""

    __tablename__ = "approval_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("dec"))
    request_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"), index=True)
    decision: Mapped[str] = mapped_column(String(16))                  # approve / reject / revise
    decided_by: Mapped[str] = mapped_column(String(128))
    comment: Mapped[str] = mapped_column(Text, default="")             # 拒绝原因结构化入库（反馈闭环输入）
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    request: Mapped["ApprovalRequest"] = relationship(back_populates="decisions")
