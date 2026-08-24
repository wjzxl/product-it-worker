"""产品域 SOP 流程（LangGraph）：澄清 → 起草 PRD → G1 人审挂起 → 建 Issue。

对齐 02-核心SOP流程设计.md §1 与 contracts/sop-example.yaml。
人审挂起用 LangGraph `interrupt()`：进程重启不丢状态（checkpoint），
Phase 0 用 MemorySaver，生产换持久化 checkpointer（Postgres/Redis）。
"""
from __future__ import annotations

import operator
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class FlowState(TypedDict, total=False):
    requirement: str
    title: str
    project_id: str
    path_with_namespace: str
    task_ref: str
    structured: str
    prd_doc: str
    version: int
    approval_id: str
    gate_decision: str
    gate_comment: str
    issue: dict
    logs: Annotated[list[str], operator.add]


class FlowContext:
    def __init__(self, llm, approval_api, gitlab_api):
        self.llm = llm
        self.approval_api = approval_api
        self.gitlab_api = gitlab_api


def _build_approval_payload(state: FlowState, version: int) -> dict:
    """构造 G1 审批请求（三要素：做了什么/依据/风险自评，schema 层强校验）。"""
    return {
        "type": "GATE_REQUIREMENT_REVIEW",
        "stage": "产品域 G1",
        "requester": f"产品Agent@{state.get('task_ref', 'run')}",
        "task_ref": state.get("task_ref", ""),
        "context_snapshot": {
            "artifact_ref": f"PRD v{version}",
            "diff_summary": f"起草 PRD v{version}：{state.get('title') or state.get('requirement', '')[:60]}",
            "evidence": [
                f"需求原文：{state.get('requirement', '')[:80]}",
                "知识库：历史需求检索结果（Phase 0 占位，T0.6 接入后替换）",
            ],
            "risk": {"score": 40, "level": "L2", "factors": ["影响面:纯新增", "不可逆:可回滚"]},
            "self_review": "验收标准已 EARS 化；边界情况待评审补充",
        },
        "options": ["approve", "reject", "revise"],
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "escalation_policy": {
            "remind_after_hours": 24,
            "escalate_to": "team_lead",
            "timeout_action": "reject",
        },
    }


def build_graph(ctx: FlowContext):
    def clarify(state: FlowState) -> dict:
        structured = (
            f"需求澄清结果：\n- 原始诉求：{state['requirement']}\n"
            f"- 目标用户/场景：待补充（Phase 0 单轮澄清）\n- 非目标：本迭代不含移动端"
        )
        return {"structured": structured, "logs": ["step:clarify done"]}

    def draft_prd(state: FlowState) -> dict:
        version = state.get("version", 0) + 1
        feedback = state.get("gate_comment", "")
        prompt = (
            f"根据以下需求起草 PRD v{version}（含 EARS 验收标准）：\n{state['structured']}"
            + (f"\n\n人审意见：{feedback}\n请按意见修订。" if feedback else "")
        )
        prd = ctx.llm.chat([{"role": "user", "content": prompt}])
        # 起草完成后立即发起 G1 审批（三要素），审批单 ID 随状态快照对外可见
        approval = ctx.approval_api.create_approval(_build_approval_payload(state, version))
        return {
            "prd_doc": prd,
            "version": version,
            "approval_id": approval.get("id", ""),
            "logs": [f"step:draft_prd v{version} done (approval={approval.get('id','')})"],
        }

    def gate_g1(state: FlowState) -> dict:
        # 挂起点：等待人审决策后从断点恢复（Command(resume=...)）
        decision = interrupt(
            {
                "gate": "G1_REQUIREMENT_REVIEW",
                "approval_id": state.get("approval_id", ""),
                "prd_version": state.get("version"),
            }
        )
        decision = decision or {}
        return {
            "gate_decision": decision.get("decision", "reject"),
            "gate_comment": decision.get("comment", ""),
            "logs": [f"gate:g1 decision={decision.get('decision')}"],
        }

    def create_issues(state: FlowState) -> dict:
        issue = ctx.gitlab_api.create_issue(
            project_id=state.get("project_id", "sandbox/prodesgin"),
            title=state.get("title") or f"需求：{state.get('requirement', '')[:40]}",
            description=state["prd_doc"],
            labels=["product", "G1-passed"],
        )
        return {"issue": issue, "logs": [f"step:create_issues done (iid={issue.get('iid')})"]}

    def reject_end(state: FlowState) -> dict:
        return {"logs": [f"gate:g1 rejected：{state.get('gate_comment', '')}"]}

    def route(state: FlowState) -> str:
        decision = state.get("gate_decision")
        if decision == "approve":
            return "create_issues"
        if decision == "revise":
            return "draft_prd"
        return "reject_end"

    builder = StateGraph(FlowState)
    builder.add_node("clarify", clarify)
    builder.add_node("draft_prd", draft_prd)
    builder.add_node("gate_g1", gate_g1)
    builder.add_node("create_issues", create_issues)
    builder.add_node("reject_end", reject_end)
    builder.add_edge(START, "clarify")
    builder.add_edge("clarify", "draft_prd")
    builder.add_edge("draft_prd", "gate_g1")
    builder.add_conditional_edges(
        "gate_g1", route,
        {"create_issues": "create_issues", "draft_prd": "draft_prd", "reject_end": "reject_end"},
    )
    builder.add_edge("create_issues", END)
    builder.add_edge("reject_end", END)

    return builder.compile(checkpointer=MemorySaver())
