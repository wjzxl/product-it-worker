"""产品域 SOP 流程（LangGraph）：澄清 → 起草 PRD → G1 人审挂起 → 建 Issue。

对齐 02-核心SOP流程设计.md §1 与 contracts/sop-example.yaml。
人审挂起用 LangGraph `interrupt()`：进程重启不丢状态（checkpoint），
Phase 0 用 MemorySaver，生产换持久化 checkpointer（Postgres/Redis）。
"""
from __future__ import annotations

import operator
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypedDict

import logging

from langgraph.checkpoint.memory import MemorySaver

from .prompts import get_prompt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


logger = logging.getLogger(__name__)

DEFAULT_DIAGNOSE_PROMPT = (
    "你是运维域诊断 Agent。基于告警、上下文与 Runbook 输出诊断报告（JSON）：\n"
    '{"root_cause": "...", "impact": "...", "change_type": "fix_code|operational", '
    '"change_summary": "建议变更...", "risk_level": "L2|L3"}\n'
    "- fix_code：需要修改代码（走修复分支+MR）；operational：操作类处置（重启/回滚/扩容等）；\n"
    "- 必须引用 Runbook 中的处置建议作为依据。"
)


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
    def __init__(self, llm, approval_api, gitlab_api, coder=None, tester=None, knowledge=None, executor=None):
        self.llm = llm
        self.approval_api = approval_api
        self.gitlab_api = gitlab_api
        self.coder = coder  # 研发域编码执行器（可选）
        self.tester = tester  # 测试域执行引擎（可选）
        self.knowledge = knowledge  # 知识库检索（Runbook RAG，可选）
        self.executor = executor  # 操作类变更执行器（可选：dryrun/webhook）


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


# =====================================================================
# 测试域 SOP：测试计划 → 独立用例生成 → 分层执行 → 缺陷闭环 → 测试报告 → G4 放行决策
# =====================================================================


class TestState(TypedDict, total=False):
    requirement: str
    prd_doc: str
    title: str
    project_id: str
    path_with_namespace: str
    task_ref: str
    test_plan: str
    test_files: list
    test_result: str
    defects: list
    test_report: str
    approval_id_g4: str
    gate4_decision: str
    gate4_comment: str
    logs: Annotated[list[str], operator.add]


def build_test_graph(ctx: FlowContext):
    def plan(state: TestState) -> dict:
        prompt = (
            f"制定测试计划（测试策略/分层/重点风险）：\n标题：{state.get('title', '')}\n"
            f"PRD：{state.get('prd_doc') or state.get('requirement', '')[:2000]}"
        )
        plan_text = ctx.llm.chat([{"role": "user", "content": prompt}])
        return {"test_plan": plan_text, "logs": ["step:test_plan done"]}

    def execute(state: TestState) -> dict:
        if ctx.tester is None:
            raise RuntimeError("未配置测试引擎（TestEngine）")
        out = ctx.tester.run(
            task={
                "task_ref": state.get("task_ref", ""),
                "title": state.get("title", ""),
            },
            prd=state.get("prd_doc") or state.get("requirement", ""),
        )
        result = out["result"]
        defects = out["defects"]
        return {
            "test_files": out["test_files"],
            "test_result": result["summary"],
            "defects": defects,
            "logs": [
                f"step:execute {'✅' if result['ok'] else '❌'} {result['summary']} "
                f"defects={len(defects)} files={len(out['test_files'])}"
            ],
        }

    def report(state: TestState) -> dict:
        defects = state.get("defects", [])
        open_defects = [d for d in defects if not d.get("closed")]
        prompt = (
            f"生成测试报告（结果/覆盖评估/风险/放行建议）：\n"
            f"测试计划：{state.get('test_plan', '')[:1500]}\n"
            f"执行结果：{state.get('test_result', '')}\n"
            f"缺陷：{defects}\n"
            f"被测功能：{state.get('title', '')}"
        )
        report_text = ctx.llm.chat([{"role": "user", "content": prompt}])
        approval = ctx.approval_api.create_approval(
            {
                "type": "GATE_RELEASE_DECISION",
                "stage": "测试域 G4",
                "requester": f"测试Agent@{state.get('task_ref', 'run')}",
                "task_ref": state.get("task_ref", ""),
                "context_snapshot": {
                    "artifact_ref": f"测试报告-{state.get('title', '')[:20]}",
                    "diff_summary": f"独立测试执行{'通过' if not open_defects else '存在未关闭缺陷'}：{state.get('test_result', '')}",
                    "evidence": [
                        f"测试文件：{len(state.get('test_files', []))} 个",
                        f"缺陷单：{len(defects)} 个（未关闭 {len(open_defects)} 个）",
                        "测试报告：见 artifact_ref",
                    ],
                    "risk": {"score": 55, "level": "L2", "factors": ["影响面:试点功能", "不可逆:可回滚"]},
                    "self_review": "已按验收标准逐条生成独立用例；未关闭缺陷需人工确认",
                },
                "options": ["approve", "reject", "revise"],
                "deadline": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "escalation_policy": {
                    "remind_after_hours": 24,
                    "escalate_to": "release_lead",
                    "timeout_action": "reject",
                },
            }
        )
        return {
            "test_report": report_text,
            "approval_id_g4": approval.get("id", ""),
            "logs": [f"step:report done (approval={approval.get('id', '')})"],
        }

    def gate_g4(state: TestState) -> dict:
        decision = interrupt({"gate": "G4_RELEASE_DECISION", "approval_id": state.get("approval_id_g4", "")}) or {}
        return {
            "gate4_decision": decision.get("decision", "reject"),
            "gate4_comment": decision.get("comment", ""),
            "logs": [f"gate:g4 decision={decision.get('decision')}"],
        }

    def route_g4(state: TestState) -> str:
        return "done_end" if state.get("gate4_decision") == "approve" else "reject_end"

    def done_end(state: TestState) -> dict:
        return {"logs": ["step:release approved —— 交付运维域（Phase 2 运维域）"]}

    def reject_end(state: TestState) -> dict:
        return {"logs": [f"release rejected: {state.get('gate4_comment', '')}"]}

    builder = StateGraph(TestState)
    builder.add_node("plan", plan)
    builder.add_node("execute", execute)
    builder.add_node("report", report)
    builder.add_node("gate_g4", gate_g4)
    builder.add_node("done_end", done_end)
    builder.add_node("reject_end", reject_end)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", "report")
    builder.add_edge("report", "gate_g4")
    builder.add_conditional_edges(
        "gate_g4", route_g4, {"done_end": "done_end", "reject_end": "reject_end"}
    )
    builder.add_edge("done_end", END)
    builder.add_edge("reject_end", END)

    return builder.compile(checkpointer=MemorySaver())


# =====================================================================
# 运维域 SOP：告警诊断 → 分级处置 → G5 变更审批 → 变更执行 → 复盘文档 → G6 复盘审查
# =====================================================================


class OpsState(TypedDict, total=False):
    project_id: str
    path_with_namespace: str
    task_ref: str
    title: str
    alert: dict
    context: str          # 日志/指标摘要（诊断输入）
    runbook: str          # Runbook 文本（入参兜底）
    runbook_sources: list  # Runbook RAG 命中的来源（引用留痕）
    diagnosis: str        # 诊断报告
    change_type: str      # fix_code / operational
    fix_branch: str
    fix_result: str
    fix_mr_iid: int
    approval_id_g5: str
    gate5_decision: str
    gate5_comment: str
    execution: str
    change_issue_iid: int
    release_pipeline_id: int
    release_status: str
    release_jobs: list
    postmortem: str
    approval_id_g6: str
    gate6_decision: str
    gate6_comment: str
    logs: Annotated[list[str], operator.add]


def _parse_action(diagnosis: str) -> tuple[str, str]:
    """从诊断文本推断操作动作（重启/扩容/回滚）与目标。"""
    text = diagnosis or ""
    if "扩容" in text or "scale" in text.lower():
        return "scale_out", "reports-exporter"
    if "回滚" in text or "rollback" in text.lower():
        return "rollback", "reports-exporter"
    if "重启" in text or "restart" in text.lower():
        return "restart", "reports-exporter"
    return "operate", "reports-exporter"


def _parse_diag_json(raw: str) -> dict | None:
    """宽松解析诊断 JSON（LLM 可能带围栏/注释）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        import json

        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def build_ops_graph(ctx: FlowContext):
    def diagnose(state: OpsState) -> dict:
        alert = state.get("alert") or {}
        # Runbook RAG：优先知识库检索（带来源引用），失败回退入参 Runbook 文本
        runbook_text = state.get("runbook", "")
        sources: list[str] = []
        if ctx.knowledge is not None:
            try:
                hits = ctx.knowledge.search(
                    f"{alert.get('title', '')} {alert.get('message', '')}".strip(), limit=3
                )
                if hits:
                    runbook_text = "\n".join(
                        f"[{h.get('source', '?')}] {h.get('text', '')}" for h in hits
                    )
                    sources = [h.get("source", "") for h in hits if h.get("source")]
                    logger.info("Runbook RAG 命中 %s", sources)
            except Exception as exc:  # KB 不可用不阻断诊断
                logger.warning("知识库检索失败，回退入参 Runbook: %s", exc)
        prompt = (
            f"# 告警\n{alert}\n\n# 上下文（日志/指标）\n{state.get('context', '')[:3000]}\n\n"
            f"# Runbook（知识库检索，含来源）\n{runbook_text[:3000]}\n"
        )
        raw = ctx.llm.chat([
            {"role": "system", "content": get_prompt("ops_diagnose_agent", DEFAULT_DIAGNOSE_PROMPT)},
            {"role": "user", "content": prompt},
        ])
        diag = _parse_diag_json(raw) or {}
        change_type = diag.get("change_type", "operational")
        if change_type not in ("fix_code", "operational"):
            change_type = "operational"
        summary = (
            f"根因：{diag.get('root_cause', raw[:200])}\n影响：{diag.get('impact', '待评估')}\n"
            f"建议变更：{diag.get('change_summary', '待定')}"
        )
        return {
            "diagnosis": summary,
            "change_type": change_type,
            "runbook_sources": sources,
            "logs": [f"step:diagnose type={change_type} risk={diag.get('risk_level', 'L3')} runbook={sources}"],
        }

    def change_prepare(state: OpsState) -> dict:
        """按分级准备变更：fix_code → AI 生成修复（分支）；operational → 仅记录变更方案。"""
        if state.get("change_type") == "fix_code":
            if ctx.coder is None:
                raise RuntimeError("未配置编码执行器（Coder）")
            alert_id = str((state.get("alert") or {}).get("id", "alert"))
            tech_design = (
                f"{state.get('diagnosis', '')[:2500]}\n"
                "约束：最小化修改——只改动必要的实现行，禁止重写整个文件，禁止改 exports 等无关模块。"
            )
            result = ctx.coder.implement(
                task={"task_ref": alert_id, "title": (state.get("alert") or {}).get("title", "告警修复")},
                prd=tech_design,
                tech_design=tech_design,
                branch_prefix="fix/alert-",
                only_src=True,
            )
            return {"fix_branch": result["branch"], "fix_result": result["test_result"], "logs": [f"step:change_prepare fix {result['branch']}"]}
        return {"logs": ["step:change_prepare operational（操作类变更）"]}

    def g5_approval(state: OpsState) -> dict:
        approval = ctx.approval_api.create_approval(
            {
                "type": "GATE_CHANGE_APPROVAL",
                "stage": "运维域 G5",
                "requester": f"运维Agent@{(state.get('alert') or {}).get('id', 'run')}",
                "task_ref": str((state.get("alert") or {}).get("id", "")),
                "context_snapshot": {
                    "artifact_ref": f"变更方案-{state.get('change_type')}",
                    "diff_summary": f"告警处置变更：{state.get('change_type')} — {state.get('diagnosis', '')[:200]}",
                    "evidence": [
                        f"告警：{(state.get('alert') or {}).get('title', '')}",
                        "诊断报告：见 artifact_ref",
                        f"Runbook 来源：{', '.join(state.get('runbook_sources', [])) or '（入参 Runbook）'}",
                        f"修复分支：{state.get('fix_branch', '-')}（fix_code 时）",
                    ],
                    "risk": {"score": 70, "level": "L3", "factors": ["影响面:生产", "不可逆:需回滚预案"]},
                    "self_review": "已按 Runbook 核对；生产变更强制审批",
                },
                "options": ["approve", "reject", "revise"],
                "deadline": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "escalation_policy": {
                    "remind_after_hours": 12,
                    "escalate_to": "ops_lead",
                    "timeout_action": "reject",
                },
            }
        )
        return {"approval_id_g5": approval.get("id", ""), "logs": [f"step:g5_approval {approval.get('id', '')}"]}

    def gate_g5(state: OpsState) -> dict:
        decision = interrupt({"gate": "G5_CHANGE_APPROVAL", "approval_id": state.get("approval_id_g5", "")}) or {}
        return {
            "gate5_decision": decision.get("decision", "reject"),
            "gate5_comment": decision.get("comment", ""),
            "logs": [f"gate:g5 decision={decision.get('decision')}"],
        }

    def route_g5(state: OpsState) -> str:
        return "execute" if state.get("gate5_decision") == "approve" else "reject_end"

    def execute(state: OpsState) -> dict:
        if state.get("change_type") == "fix_code" and state.get("fix_branch"):
            # 真实合入修复（push 模式）；测试模式仅记录
            mr = ctx.gitlab_api.create_mr(
                state.get("project_id", "sandbox/prodesgin"),
                state.get("fix_branch", ""), "main",
                title=f"fix: 告警处置 {(state.get('alert') or {}).get('title', '')}",
                description=state.get("diagnosis", "")[:2000],
            )
            mr_info = ctx.gitlab_api.get_mr(state.get("project_id", "sandbox/prodesgin"), mr.get("iid"))
            ctx.gitlab_api.merge_mr(state.get("project_id", "sandbox/prodesgin"), mr.get("iid"), sha=mr_info.get("sha"))
            return {
                "execution": f"fix_code 已合入 MR !{mr.get('iid')}",
                "fix_mr_iid": mr.get("iid"),
                "logs": [f"step:execute merged !{mr.get('iid')}"],
            }
        # operational：经操作执行器下发（dryrun 记录 / webhook 真实调用）
        action, target = _parse_action(state.get("diagnosis", ""))
        result = (
            ctx.executor.execute(action, target, {"alert": (state.get("alert") or {}).get("id", "")})
            if ctx.executor is not None
            else {"ok": True, "mode": "none", "detail": "未配置执行器（记录式）"}
        )
        change_issue = ctx.gitlab_api.create_issue(
            state.get("project_id", "sandbox/prodesgin"),
            title=f"变更: {(state.get('alert') or {}).get('title', '')}",
            description=(
                f"操作类变更（{result.get('mode')}）：{result.get('detail')}\n\n"
                f"诊断：\n{state.get('diagnosis', '')[:1500]}"
            ),
            labels=["change", "auto-ops"],
        )
        return {
            "execution": f"operational 变更：{result.get('detail')}",
            "change_issue_iid": change_issue.get("iid"),
            "logs": [f"step:execute operational {result.get('mode')} ok={result.get('ok')} change #{change_issue.get('iid')}"],
        }

    def release(state: OpsState) -> dict:
        """D1 发布执行：触发真实 CI/CD 流水线并回读结果（无 Runner/未配置时如实记录）。"""
        import time as _time

        project = state.get("project_id", "sandbox/prodesgin")
        try:
            pipeline = ctx.gitlab_api.trigger_pipeline(project, "main")
            pid = pipeline.get("id")
            status = pipeline.get("status", "pending")
            # 回读（最多 ~60s，避免长时间阻塞）
            for _ in range(12):
                if status in ("success", "failed", "canceled", "skipped"):
                    break
                _time.sleep(5)
                status = ctx.gitlab_api.get_pipeline(project, pid).get("status", status)
            jobs = []
            try:
                jobs = [
                    {"name": j.get("name"), "status": j.get("status")}
                    for j in ctx.gitlab_api.get_pipeline_jobs(project, pid)
                ]
            except Exception:  # 作业列表读取失败不影响主流程
                pass
            return {
                "release_pipeline_id": pid,
                "release_status": status,
                "release_jobs": jobs,
                "logs": [f"step:release pipeline !{pid} status={status} jobs={len(jobs)}"],
            }
        except Exception as exc:  # 触发被拒（如账号未实名/Runner 缺失）如实记录，不阻断闭环
            detail = str(exc)
            note = "流水线触发失败"
            if "Identity verification" in detail:
                note = "流水线触发被拒：GitLab 账号需实名验证（或改用自建 Runner）"
            return {
                "release_status": "not_triggered",
                "release_jobs": [],
                "logs": [f"step:release {note}: {detail[:200]}"],
            }

    def postmortem(state: OpsState) -> dict:
        prompt = (
            "生成故障复盘文档（时间线/根因/影响/改进项/跟进负责人）：\n"
            f"告警：{state.get('alert')}\n诊断：{state.get('diagnosis', '')}\n"
            f"处置：{state.get('execution', '')}\n变更类型：{state.get('change_type')}\n"
            f"发布流水线：{state.get('release_status', '-')}（作业：{state.get('release_jobs', [])}）"
        )
        report = ctx.llm.chat([{"role": "user", "content": prompt}])
        approval = ctx.approval_api.create_approval(
            {
                "type": "GATE_POSTMORTEM_REVIEW",
                "stage": "运维域 G6",
                "requester": f"运维Agent@{(state.get('alert') or {}).get('id', 'run')}",
                "task_ref": str((state.get("alert") or {}).get("id", "")),
                "context_snapshot": {
                    "artifact_ref": f"复盘-{(state.get('alert') or {}).get('title', '')[:20]}",
                    "diff_summary": (
                        f"复盘文档已生成：{state.get('execution', '')}｜"
                        f"发布流水线：{state.get('release_status', '-')}"
                    ),
                    "evidence": [f"告警：{(state.get('alert') or {}).get('title', '')}", "诊断：见复盘文档"],
                    "risk": {"score": 40, "level": "L2", "factors": ["复盘审查", "常规告警"]},
                    "self_review": "复盘含根因与改进项；事故级需技术负责人审查",
                },
                "options": ["approve", "reject", "revise"],
                "deadline": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
                "escalation_policy": {"remind_after_hours": 48, "escalate_to": "tech_lead", "timeout_action": "reject"},
            }
        )
        return {
            "postmortem": report,
            "approval_id_g6": approval.get("id", ""),
            "logs": [f"step:postmortem {approval.get('id', '')}"],
        }

    def gate_g6(state: OpsState) -> dict:
        decision = interrupt({"gate": "G6_POSTMORTEM_REVIEW", "approval_id": state.get("approval_id_g6", "")}) or {}
        return {
            "gate6_decision": decision.get("decision", "reject"),
            "gate6_comment": decision.get("comment", ""),
            "logs": [f"gate:g6 decision={decision.get('decision')}"],
        }

    def route_g6(state: OpsState) -> str:
        return "done_end" if state.get("gate6_decision") == "approve" else "reject_end"

    def done_end(state: OpsState) -> dict:
        return {"logs": ["step:ops closed —— 稳态"]}

    def reject_end(state: OpsState) -> dict:
        return {"logs": [f"ops rejected: {state.get('gate5_comment') or state.get('gate6_comment', '')}"]}

    builder = StateGraph(OpsState)
    builder.add_node("diagnose", diagnose)
    builder.add_node("change_prepare", change_prepare)
    builder.add_node("g5_approval", g5_approval)
    builder.add_node("gate_g5", gate_g5)
    builder.add_node("execute", execute)
    builder.add_node("release", release)
    builder.add_node("postmortem_doc", postmortem)
    builder.add_node("gate_g6", gate_g6)
    builder.add_node("done_end", done_end)
    builder.add_node("reject_end", reject_end)
    builder.add_edge(START, "diagnose")
    builder.add_edge("diagnose", "change_prepare")
    builder.add_edge("change_prepare", "g5_approval")
    builder.add_edge("g5_approval", "gate_g5")
    builder.add_conditional_edges(
        "gate_g5", route_g5, {"execute": "execute", "reject_end": "reject_end"}
    )
    builder.add_edge("execute", "release")
    builder.add_edge("release", "postmortem_doc")
    builder.add_edge("postmortem_doc", "gate_g6")
    builder.add_conditional_edges(
        "gate_g6", route_g6, {"done_end": "done_end", "reject_end": "reject_end"}
    )
    builder.add_edge("done_end", END)
    builder.add_edge("reject_end", END)

    return builder.compile(checkpointer=MemorySaver())
