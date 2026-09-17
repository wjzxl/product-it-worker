"""审批领域模型。

状态机（v0）：
    pending ──approve──> approved
    pending ──reject───> rejected
    pending ──revise───> revised ──(修订后重新提交, version+1)──> pending
    pending ──超时─────> rejected（L3 默认拒绝，绝不静默放行）
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

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
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)  # 多租户隔离键
    type: Mapped[str] = mapped_column(String(64), index=True)          # GATE_REQUIREMENT_REVIEW ...
    stage: Mapped[str] = mapped_column(String(128))                    # 如 "产品域 G1"
    requester: Mapped[str] = mapped_column(String(128))                # 如 "产品Agent@issue-1234"
    task_ref: Mapped[str] = mapped_column(String(128), default="")

    # 三要素上下文快照（创建后不可变，审批人看到一致视图）
    context_snapshot: Mapped[dict] = mapped_column(JSON)

    # WP1 需求来源链：从 context_snapshot.sources 冗余出的独立列，便于按来源反查
    # （"这条需求当初是谁、在哪句话里提出的"）。空列表=存量数据，不视为缺失。
    sources: Mapped[list] = mapped_column(JSON, default=list)

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


class TrustState(Base):
    """Gate 信任状态（惩罚期）：退回/否决后强制 L3，到期按指标重新计算。"""

    __tablename__ = "trust_state"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)  # Gate 类型
    penalty_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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


class GateCheck(Base):
    """工程闸门检查留痕（WP2，E1–E3）。

    与 `ApprovalRequest` 的区别：**E 闸门是机器判定**（规则即代码，判定器可单测），
    G 闸门是人做业务决策。两者共同构成双层闸门：E 管"实施条件是否具备"，G 管"业务是否同意"。
    """

    __tablename__ = "gate_checks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("gck"))
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)          # E1 / E2 / E3
    task_ref: Mapped[str] = mapped_column(String(128), default="", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="warn")      # off / warn / block
    passed: Mapped[bool] = mapped_column(default=False)
    blocked: Mapped[bool] = mapped_column(default=False)               # 实际是否阻断了推进
    gaps: Mapped[list] = mapped_column(JSON, default=list)             # 缺口清单（结构化）
    context: Mapped[dict] = mapped_column(JSON, default=dict)          # 被检查的上下文快照
    rule_version: Mapped[str] = mapped_column(String(64), default="")
    checked_by: Mapped[str] = mapped_column(String(128), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # WP10 / M1 决策 D3：误拦反馈（人判断"这次其实该放行"）
    # —— 计划 §8 的误拦率 <10% 是硬指标，没有这个字段就永远算不出来
    feedback: Mapped[str] = mapped_column(String(32), default="")        # ""/valid/false_positive
    feedback_note: Mapped[str] = mapped_column(Text, default="")
    feedback_by: Mapped[str] = mapped_column(String(128), default="")
    feedback_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceArtifact(Base):
    """证据工件（WP5）：验收项 ↔ 可复验材料。

    对应课程图 33：需求、代码产物与测试证据要互相对应，半年后接手的人要能复验。
    元数据在 PG（租户隔离 + 审计），工件本体在本地卷（EVIDENCE_DIR，可换对象存储）。
    """

    __tablename__ = "evidence_artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("evd"))
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    task_ref: Mapped[str] = mapped_column(String(128), default="", index=True)
    acceptance_item: Mapped[str] = mapped_column(String(128), default="", index=True)  # 对应哪个验收项
    kind: Mapped[str] = mapped_column(String(32), default="other")                     # export/log/screenshot/...
    filename: Mapped[str] = mapped_column(String(200), default="")
    path: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")                        # 复验用
    size: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
