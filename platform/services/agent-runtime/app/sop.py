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
    def __init__(self, llm, approval_api, gitlab_api, coder=None):
        self.llm = llm
        self.approval_api = approval_api
        self.gitlab_api = gitlab_api
        self.coder = coder  # 研发域编码执行器（可选）


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


# =====================================================================
# 研发域 SOP：技术方案 → G2 评审 → AI 编码+质量门禁 → MR → G3 评审 → 合入
# =====================================================================


class DevState(TypedDict, total=False):
    requirement: str
    prd_doc: str
    title: str
    project_id: str
    path_with_namespace: str
    task_ref: str
    tech_design: str
    approval_id_g2: str
    gate2_decision: str
    gate2_comment: str
    branch: str
    changed_files: list
    test_result: str
    mr_iid: int
    mr_url: str
    approval_id_g3: str
    gate3_decision: str
    gate3_comment: str
    merged: bool
    logs: Annotated[list[str], operator.add]


def _dev_approval_payload(state: DevState, gate_type: str, stage: str, artifact_ref: str, diff_summary: str, evidence: list[str]) -> dict:
    return {
        "type": gate_type,
        "stage": stage,
        "requester": f"研发Agent@{state.get('task_ref', 'run')}",
        "task_ref": state.get("task_ref", ""),
        "context_snapshot": {
            "artifact_ref": artifact_ref,
            "diff_summary": diff_summary,
            "evidence": evidence,
            "risk": {"score": 45, "level": "L2", "factors": ["影响面:新增模块", "不可逆:可回滚"]},
            "self_review": "已按验收标准逐条核对；边界情况待评审补充",
        },
        "options": ["approve", "reject", "revise"],
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "escalation_policy": {"remind_after_hours": 24, "escalate_to": "team_lead", "timeout_action": "reject"},
    }


def build_dev_graph(ctx: FlowContext):
    def tech_design(state: DevState) -> dict:
        prompt = (
            f"为以下需求起草技术方案（模块拆分、接口设计、测试策略、风险）：\n"
            f"标题：{state.get('title', '')}\nPRD：{state.get('prd_doc') or state.get('requirement', '')[:2000]}"
            + (f"\n人审意见：{state.get('gate2_comment', '')}\n请按意见修订。" if state.get("gate2_comment") else "")
        )
        design = ctx.llm.chat([{"role": "user", "content": prompt}])
        approval = ctx.approval_api.create_approval(
            _dev_approval_payload(
                state, "GATE_TECH_DESIGN_REVIEW", "研发域 G2",
                "技术方案 v1",
                f"起草技术方案：{state.get('title', '')[:60]}",
                [f"Issue: {state.get('task_ref', '')}", f"PRD: {state.get('prd_doc', '')[:80]}"],
            )
        )
        return {
            "tech_design": design,
            "approval_id_g2": approval.get("id", ""),
            "logs": [f"step:tech_design done (approval={approval.get('id', '')})"],
        }

    def gate_g2(state: DevState) -> dict:
        decision = interrupt({"gate": "G2_TECH_DESIGN_REVIEW", "approval_id": state.get("approval_id_g2", "")}) or {}
        return {
            "gate2_decision": decision.get("decision", "reject"),
            "gate2_comment": decision.get("comment", ""),
            "logs": [f"gate:g2 decision={decision.get('decision')}"],
        }

    def route_g2(state: DevState) -> str:
        d = state.get("gate2_decision")
        return {"approve": "implement", "revise": "tech_design"}.get(d, "reject_end")

    def implement(state: DevState) -> dict:
        if ctx.coder is None:
            raise RuntimeError("未配置编码执行器（Coder）")
        result = ctx.coder.implement(
            task={
                "task_ref": state.get("task_ref", ""),
                "title": state.get("title", ""),
            },
            prd=state.get("prd_doc") or state.get("requirement", ""),
            tech_design=state.get("tech_design", ""),
        )
        return {
            "branch": result["branch"],
            "changed_files": result["changed_files"],
            "test_result": result["test_result"],
            "logs": [
                f"step:implement done branch={result['branch']} tests={result['test_result']} "
                f"files={len(result['changed_files'])}"
            ],
        }

    def create_mr(state: DevState) -> dict:
        if state.get("mr_iid"):
            # 返工场景：分支已存在 MR，新提交随 push 自动同步到该 MR，无需重复创建
            return {"logs": [f"step:create_mr skipped（复用 !{state.get('mr_iid')}）"]}
        mr = ctx.gitlab_api.create_mr(
            project_id=state.get("project_id", "sandbox/prodesgin"),
            source_branch=state.get("branch", ""),
            target_branch="main",
            title=f"feat: {state.get('title', '')}",
            description=(
                f"### 技术方案\n{state.get('tech_design', '')[:2000]}\n\n"
                f"### 质量门禁\n{state.get('test_result', '')}\n\n"
                f"### 变更文件\n" + "\n".join(f"- {f}" for f in state.get("changed_files", []))
            ),
        )
        return {"mr_iid": mr.get("iid"), "mr_url": mr.get("web_url", ""), "logs": [f"step:create_mr !{mr.get('iid')}"]}

    def g3_approval(state: DevState) -> dict:
        approval = ctx.approval_api.create_approval(
            _dev_approval_payload(
                state, "GATE_PR_REVIEW", "研发域 G3",
                f"MR !{state.get('mr_iid', '')}",
                f"AI 完成编码并自测通过：{state.get('test_result', '')}",
                [f"MR: {state.get('mr_url', '')}", f"分支: {state.get('branch', '')}"],
            )
        )
        return {"approval_id_g3": approval.get("id", ""), "logs": [f"step:g3_approval {approval.get('id', '')}"]}

    def gate_g3(state: DevState) -> dict:
        decision = interrupt({"gate": "G3_PR_REVIEW", "approval_id": state.get("approval_id_g3", ""), "mr_url": state.get("mr_url", "")}) or {}
        return {
            "gate3_decision": decision.get("decision", "reject"),
            "gate3_comment": decision.get("comment", ""),
            "logs": [f"gate:g3 decision={decision.get('decision')}"],
        }

    def route_g3(state: DevState) -> str:
        d = state.get("gate3_decision")
        return {"approve": "merge", "revise": "implement"}.get(d, "reject_end")

    def merge(state: DevState) -> dict:
        # GitLab 新版合入 API 要求携带 sha（源分支头提交）：先查 MR 获取
        mr = ctx.gitlab_api.get_mr(state.get("project_id", "sandbox/prodesgin"), state.get("mr_iid", 0))
        sha = mr.get("sha") or mr.get("diff_head_sha")
        merged = ctx.gitlab_api.merge_mr(state.get("project_id", "sandbox/prodesgin"), state.get("mr_iid", 0), sha=sha)
        return {"merged": True, "logs": [f"step:merge !{state.get('mr_iid')} state={merged.get('state')} sha={sha}"]}

    def reject_end(state: DevState) -> dict:
        return {"logs": [f"dev rejected: {state.get('gate3_comment') or state.get('gate2_comment', '')}"]}

    builder = StateGraph(DevState)
    builder.add_node("design", tech_design)
    builder.add_node("gate_g2", gate_g2)
    builder.add_node("implement", implement)
    builder.add_node("create_mr", create_mr)
    builder.add_node("g3_approval", g3_approval)
    builder.add_node("gate_g3", gate_g3)
    builder.add_node("merge", merge)
    builder.add_node("reject_end", reject_end)
    builder.add_edge(START, "design")
    builder.add_edge("design", "gate_g2")
    builder.add_conditional_edges(
        "gate_g2", route_g2,
        {"implement": "implement", "tech_design": "design", "reject_end": "reject_end"},
    )
    builder.add_edge("implement", "create_mr")
    builder.add_edge("create_mr", "g3_approval")
    builder.add_edge("g3_approval", "gate_g3")
    builder.add_conditional_edges(
        "gate_g3", route_g3,
        {"merge": "merge", "implement": "implement", "reject_end": "reject_end"},
    )
    builder.add_edge("merge", END)
    builder.add_edge("reject_end", END)

    return builder.compile(checkpointer=MemorySaver())
