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
    "GATE_PROCESS_IMPROVEMENT",
]


class RiskSnapshot(BaseModel):
    score: int = Field(ge=0, le=100)
    level: Literal["L1", "L2", "L3"]
    factors: list[str] = Field(default_factory=list)


SourceKind = Literal["chat", "voice", "image", "excel", "issue", "doc", "mail", "manual"]


class SourceRef(BaseModel):
    """需求来源引用（WP1 provenance）：让每条结论能回到原始材料的具体位置。

    对应《从 vibe coding 到 FDE 交付》图 11「每条需求都带出处」：
    消息 ID / 录音时间戳 / 图片区域 / 表格单元格——争论"这个字段为什么存在"时可回查。
    """

    kind: SourceKind = Field(description="材料载体类型")
    ref: str = Field(min_length=1, description="材料定位（如 feishu://chat/oc_x/msg/om_y、GitLab issue URL）")
    quote: str = Field(default="", max_length=2000, description="原话/原文摘录（证据的最小片段）")
    locator: dict[str, Any] = Field(
        default_factory=dict,
        description="载体内的精确位置：voice→{start_ms,end_ms}；image→{bbox:[x,y,w,h]}；excel→{sheet,cell}；chat→{msg_id}",
    )
    captured_at: Optional[datetime] = Field(default=None, description="材料产生时间（非录入时间）")
    confirmed_by: str = Field(default="", description="该出处的确认人（谁说了这句话/谁确认了这条）")


class ContextSnapshot(BaseModel):
    """三要素上下文：做了什么 / 依据 / 风险自评（缺一不可）。

    WP1 扩展：`sources` 为**可选**字段（默认空），存量审批与既有测试零破坏；
    新流程（需求澄清 / 工程闸门 E1）会要求其非空。
    """

    artifact_ref: str = Field(min_length=1)
    diff_summary: str = Field(min_length=1, description="① 做了什么")
    evidence: list[str] = Field(min_length=1, description="② 依据链接")
    risk: RiskSnapshot = Field(description="③ 风险自评")
    self_review: str = ""
    sources: list[SourceRef] = Field(default_factory=list, description="④ 需求来源链（WP1）")


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
    tenant: str = "default"
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
