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
        """安全底线校验（修复 BUG-004 的"空校验"）：超时动作只能是默认拒绝或 L1 自动放行。

        真正的等级判断在 sweeper 里用**服务端信任等级**完成（见 app/policies.py）；
        这里保证契约层不会出现第三种取值，也不会出现"未声明却自动放行"。
        """
        if self.timeout_action not in ("reject", "auto_approve_l1_only"):
            raise ValueError("timeout_action 只允许 reject 或 auto_approve_l1_only")
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


class GateCheckIn(BaseModel):
    """工程闸门检查请求（WP2）。上下文交由判定器按规则取用，故为自由结构。

    **安全约束（修复 BUG-003/014）**：`mode` 与 `persist` **不接受客户端覆盖**——
    闸门宽严由服务端规则文件 + `ENG_GATES_MODE` 决定，留痕一律强制。
    否则调用方可用 `mode=off` 自免门禁、用 `persist=false` 规避审计。
    这两个字段若被传入会被忽略（并记录告警），保留字段定义只为兼容旧客户端。
    """

    task_ref: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    checked_by: str = ""
    # 以下两个字段：**已废弃，传入即被忽略**（见上方安全约束）
    mode: Optional[Literal["off", "warn", "block"]] = Field(
        default=None, deprecated=True, description="已废弃：不接受客户端覆盖，忽略")
    persist: Optional[bool] = Field(
        default=None, deprecated=True, description="已废弃：闸门检查一律留痕，忽略")


class GateFeedbackIn(BaseModel):
    """误拦反馈（WP10）：人判断这次拦截是否其实该放行。"""

    verdict: Literal["valid", "false_positive"]
    note: str = ""
    by: str = ""


class GateCheckOut(BaseModel):
    """判定结果即契约：blocked=true 时调用方**必须**停止推进。"""

    check_id: str = ""
    code: str
    title: str = ""
    mode: str
    passed: bool
    blocked: bool
    gaps: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    critical_missing: list[str] = []
    rule_version: str = ""
    fingerprint: str = ""
    source_path: str = ""


class ImCardCallback(BaseModel):
    """IM 交互卡片回调：卡片按钮 → 决策。"""

    approval_id: str
    action: DecisionValue
    operator: str
    comment: str = ""
