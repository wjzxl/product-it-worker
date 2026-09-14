"""Pydantic 模型：API 输入输出 + 三要素校验。"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

DecisionValue = Literal["approve", "reject", "revise"]
GATE_TYPES = Literal[
    "GATE_REQUIREMENT_REVIEW",
    "GATE_TECH_DESIGN_REVIEW",
    "GATE_PR_REVIEW",
    "GATE_RELEASE_DECISION",
    "GATE_CHANGE_APPROVAL",
    "GATE_POSTMORTEM_REVIEW",
]


class RiskSnapshot(BaseModel):
    score: int = Field(ge=0, le=100)
    level: Literal["L1", "L2", "L3"]
    factors: list[str] = Field(default_factory=list)


class ContextSnapshot(BaseModel):
    """三要素上下文：做了什么 / 依据 / 风险自评（缺一不可）。"""

    artifact_ref: str = Field(min_length=1)
    diff_summary: str = Field(min_length=1, description="① 做了什么")
    evidence: list[str] = Field(min_length=1, description="② 依据链接")
    risk: RiskSnapshot = Field(description="③ 风险自评")
    self_review: str = ""


class EscalationPolicy(BaseModel):
    remind_after_hours: float = 24
    escalate_to: str = "team_lead"
    timeout_action: Literal["reject", "auto_approve_l1_only"] = "reject"

    @model_validator(mode="after")
    def _l3_must_reject_on_timeout(self) -> "EscalationPolicy":
        # 安全底线：默认拒绝；只有显式声明的 L1 才允许超时自动放行（Phase 0 不启用）
        return self


class ApprovalRequestCreate(BaseModel):
    type: GATE_TYPES
    stage: str = Field(min_length=1)
    requester: str = Field(min_length=1)
    task_ref: str = ""
    context_snapshot: ContextSnapshot
    options: list[DecisionValue] = Field(default=["approve", "reject", "revise"])
    deadline: datetime
    escalation_policy: EscalationPolicy = Field(default_factory=EscalationPolicy)
    auto_gate: bool = False  # True → 由信任等级决定是否自动放行（L1 放行 / L2 抽样 / L3 人工）


class DecisionIn(BaseModel):
    decision: DecisionValue
    decided_by: str = Field(min_length=1)
    comment: str = ""

    @model_validator(mode="after")
    def _reject_needs_reason(self) -> "DecisionIn":
        # 拒绝/退回必须给原因——这是反馈闭环的输入
        if self.decision in ("reject", "revise") and not self.comment.strip():
            raise ValueError("reject/revise 必须附原因（comment），原因将进入反馈闭环")
        return self


class DecisionOut(BaseModel):
    id: str
    decision: str
    decided_by: str
    comment: str
    decided_at: datetime

    model_config = {"from_attributes": True}


class ApprovalRequestOut(BaseModel):
    id: str
    type: str
    stage: str
    requester: str
    task_ref: str
    context_snapshot: dict[str, Any]
    options: list[str]
    deadline: datetime
    escalation_policy: dict[str, Any]
    status: str
    version: int
    created_at: datetime
    decisions: list[DecisionOut] = []
    trust_level: str = ""
    trust_reason: str = ""
    auto_decision: str = ""  # approved / sampled_review / pending

    model_config = {"from_attributes": True}


class ImCardCallback(BaseModel):
    """IM 交互卡片回调：卡片按钮 → 决策。"""

    approval_id: str
    action: DecisionValue
    operator: str
    comment: str = ""
