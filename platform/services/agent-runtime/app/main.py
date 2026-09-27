"""Agent 运行时入口：产品域（PRD+G1）/ 研发域（方案+G2+编码+MR+G3+合入）/ 测试域（独立用例+G4 放行）。

Phase 0/1 编排方式：演示脚本驱动"启动 → 人审 → 恢复"；
Phase 2 起由工作流引擎（n8n/Temporal）消费 approval.decided 事件自动恢复。
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from . import ambiguity, gate_decisions
from . import roles as role_registry
from . import runs
from . import rules_pack
from .tools import ToolDenied, ToolError, ToolRegistry
from .apis import ApprovalAPI, GateAPI, GitLabAPI, KnowledgeAPI
from .checkpointer import build as build_checkpointer
from .checkpointer import describe as checkpointer_describe
from .sop import checkpointer_kind
from .coder import Coder
from .executors import executor_from_env
from .llm import llm_from_env
from .prompts import active_versions, get_prompt
from .sop import FlowContext, build_dev_graph, build_graph, build_ops_graph, build_test_graph
from .tester import TestEngine
from .service_auth import install_service_auth, security_posture

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

SOP_PRODUCT = "product-domain/prd-flow"
SOP_DEV = "dev-domain/issue-to-merge"
SOP_TEST = "test-domain/release-gate"
SOP_OPS = "ops-domain/alert-to-postmortem"


class RunIn(BaseModel):
    sop: str = SOP_PRODUCT
    requirement_text: str = Field(min_length=1)
    title: str = ""
    task_ref: str = ""
    project_id: str = "sandbox/prodesgin"
    path_with_namespace: str = ""
    prd_doc: str = ""
    sources: list[dict] = Field(default_factory=list)  # WP1 需求来源链（材料出处，可空）
    trigger: str = "manual"      # WP7 归档：manual/schedule/event/alert
    tenant: str = "default"      # WP7 归档 + 多产品线统计
    role: str = ""               # WP7 归档：岗位（空则按 SOP 推断）
    alert: dict = Field(default_factory=dict)   # 运维域：告警载荷
    context: str = ""       # 运维域：日志/指标上下文
    runbook: str = ""       # 运维域：Runbook 文本


class ToolInvokeIn(BaseModel):
    """工具调用请求（WP8）：role 用于执行期鉴权。"""

    tool: str
    params: dict = Field(default_factory=dict)
    role: str = ""
    approval_ref: str = ""        # 写类工具需带人审依据
    allow_ungated: bool = False   # 仅用于演练/平台内部，需显式开启


class WakeIn(BaseModel):
    """数据/条件就绪后唤醒等待中的运行（WP7）。"""

    data: dict = Field(default_factory=dict)   # 到齐的数据（如指标/日志）
    comment: str = ""


class ResumeIn(BaseModel):
    decision: str = Field(min_length=1)  # approve / reject / revise
    comment: str = ""
    # 治理专项：审批台里的决策编号。GATE_DECISION_VERIFY=block（生产默认）时必填，
    # 服务端据此向审批网关核验"这条决策真的存在且与本次动作一致"。
    approval_id: str = ""


_ROLE_BY_SOP = {
    "product-domain/prd-flow": "product-agent",
    "dev-domain/issue-to-merge": "dev-agent",
    "test-domain/release-gate": "tester-agent",
    "ops-domain/alert-to-postmortem": "ops-duty",
}


def _locate_graph(graphs: dict, thread_id: str):
    """按 thread_id 找到所属 SOP 的图（兼容尚无状态的新线程）。"""
    for name, graph in graphs.items():
        snap = graph.get_state(_config(thread_id, name))
        if snap.values or snap.next:
            return name, graph
    return None, None


def _role_of(sop: str) -> str:
    """SOP → 岗位（WP6 三件套按岗位组织，归档按岗位统计）。"""
    return _ROLE_BY_SOP.get(sop, "")


def _usage_of(ctx) -> dict:
    """LLM 用量：桩/未接计量的客户端如实返回 cost_metered=False（不冒充已计量）。"""
    if hasattr(ctx.llm, "usage"):
        return ctx.llm.usage()
    return {"cost_metered": False, "unmetered_note": f"{type(ctx.llm).__name__} 未提供用量计量"}


def _archive_progress(thread_id: str, status_body: dict, ctx, extra: Optional[dict] = None) -> None:
    """按运行状态归档：等待（人审/数据）、完成、被否决、失败。

    修复 BUG-012：此前只特判 waiting_approval/waiting_data/done，
    `rejected` 落入 else 被记成 running/waiting，导致 /runs 与 stats.success_rate 看不到否决。
    """
    status = status_body.get("status", "")
    usage = _usage_of(ctx)
    extra = extra or {}
    if status == "waiting_approval":
        # 被"退回修订"后流程回到 PRD 并再次挂起 G1，状态仍是 waiting_approval，
        # 因此"这轮是退回"只能从决策侧带进来（extra），否则归档看不出被退回过。
        runs.finish_run(thread_id, "waiting", usage, {"gate": status_body.get("gate", ""), **extra})
    elif status == "waiting_data":
        runs.mark_waiting(thread_id, status_body.get("waiting_reason", "数据未就绪"), _usage_of(ctx),
                          {**extra} or None)
    elif status == "done":
        runs.finish_run(thread_id, "done", usage, {"issue": status_body.get("issue", {}), **extra})
    elif status == "rejected":
        runs.finish_run(thread_id, "rejected", usage,
                        {"gate": status_body.get("gate", ""), "comment": status_body.get("comment", ""),
                         **extra})
    else:
        runs.finish_run(thread_id, "running", usage, {**extra} or None)


def _config(thread_id: str, sop: str = SOP_PRODUCT) -> dict:
    """LangGraph 运行配置。

    WP7：checkpoint 键必须**按 SOP 命名空间隔离**——共用持久化 checkpointer 时，
    若两个不同 SOP 用了同一个 thread_id（例如都由 task_ref 派生），状态会互相覆盖。
    对外暴露的 thread_id 保持不变，只在内部加前缀。
    """
    return {"configurable": {"thread_id": f"{sop}::{thread_id}", "sop": sop}}


def _status(graph, thread_id: str, sop: str) -> dict:
    snap = graph.get_state(_config(thread_id, sop))
    values = snap.values or {}
    next_nodes = list(snap.next or [])

    # WP7：数据等待（不是人审等待）需如实报告，否则上层无法区分"等人"与"等数据"
    if "await_data" in next_nodes:
        return {
            "thread_id": thread_id,
            "sop": sop,
            "status": "waiting_data",
            "waiting_reason": "指标/日志尚未到齐（await_data 挂起）",
            "logs": values.get("logs", []),
            "waits": values.get("waits", []),
        }

    gate = next((g for g in ("gate_g1", "gate_g2", "gate_g3", "gate_g4", "gate_g5", "gate_g6") if g in next_nodes), None)
    if gate:
        aid_key = {
            "gate_g1": "approval_id",
            "gate_g2": "approval_id_g2",
            "gate_g3": "approval_id_g3",
            "gate_g4": "approval_id_g4",
            "gate_g5": "approval_id_g5",
            "gate_g6": "approval_id_g6",
        }[gate]
        body = {
            "thread_id": thread_id,
            "sop": sop,
            "status": "waiting_approval",
            "gate": gate,
            "approval_id": values.get(aid_key, ""),
            "logs": values.get("logs", []),
        }
        if gate == "gate_g1":
            body["prd_version"] = values.get("version")
            body["prd_doc"] = values.get("prd_doc", "")
            # WP1：把澄清结果与来源链一并返回，人审与工程闸门都据此判断
            body["clarification"] = values.get("clarification", {})
            body["sources"] = values.get("sources", [])
        if gate == "gate_g2":
            body["tech_design"] = values.get("tech_design", "")
        if gate == "gate_g3":
            body["mr_iid"] = values.get("mr_iid")
            body["mr_url"] = values.get("mr_url", "")
            body["branch"] = values.get("branch", "")
            body["test_result"] = values.get("test_result", "")
        if gate == "gate_g4":
            body["test_result"] = values.get("test_result", "")
            body["test_report"] = values.get("test_report", "")
            body["defects"] = values.get("defects", [])
        if gate == "gate_g5":
            body["diagnosis"] = values.get("diagnosis", "")
            body["change_type"] = values.get("change_type", "")
            body["fix_branch"] = values.get("fix_branch", "")
            body["runbook_sources"] = values.get("runbook_sources", [])
        if gate == "gate_g6":
            body["postmortem"] = values.get("postmortem", "")
            body["execution"] = values.get("execution", "")
            body["release_status"] = values.get("release_status", "")
            body["release_pipeline_id"] = values.get("release_pipeline_id")
        return body

    if not next_nodes:
        if values.get("merged"):
            return {
                "thread_id": thread_id, "sop": sop, "status": "done",
                "merged": True,
                "mr_iid": values.get("mr_iid"), "mr_url": values.get("mr_url", ""),
                "branch": values.get("branch", ""), "test_result": values.get("test_result", ""),
                "logs": values.get("logs", []),
            }
        if (
            values.get("gate_decision") == "reject"
            or values.get("gate3_decision") == "reject"
            or values.get("gate2_decision") == "reject"
            or values.get("gate4_decision") == "reject"
            or values.get("gate5_decision") == "reject"
            or values.get("gate6_decision") == "reject"
        ):
            comment = (
                values.get("gate6_comment")
                or values.get("gate5_comment")
                or values.get("gate4_comment")
                or values.get("gate3_comment")
                or values.get("gate2_comment")
                or values.get("gate_comment", "")
            )
            return {"thread_id": thread_id, "sop": sop, "status": "rejected", "comment": comment, "logs": values.get("logs", [])}
        return {"thread_id": thread_id, "sop": sop, "status": "done", "issue": values.get("issue"), "logs": values.get("logs", [])}

    return {"thread_id": thread_id, "sop": sop, "status": "running", "next": next_nodes}


def create_app(
    approval_api=None,
    gitlab_api=None,
    llm=None,
    coder=None,
    tester=None,
    knowledge_api=None,
    executor=None,
    gate_api=None,
) -> FastAPI:
    llm = llm or llm_from_env()
    gitlab_api = gitlab_api or GitLabAPI(os.getenv("GITLAB_BRIDGE_URL", "http://127.0.0.1:8006"))
    # WP2：工程闸门客户端（E1 在生成代码前拦截）
    gate_api = gate_api if gate_api is not None else GateAPI(os.getenv("APPROVAL_GATEWAY_URL", "http://127.0.0.1:8001"))
    # WP8：工具注册表（先建表，handler 在 ctx 就绪后绑定）
    tool_registry = ToolRegistry()
    coder = coder or Coder(
        llm=llm,
        gitlab_url=os.getenv("GITLAB_URL", "https://gitlab.com"),
        gitlab_token=os.getenv("GITLAB_TOKEN", ""),
        project_path=os.getenv("DEMO_PROJECT", "sandbox/prodesgin"),
        gate_api=gate_api,
    )
    tester = tester or TestEngine(
        llm=llm,
        coder=coder,
        gitlab_api=gitlab_api,
        gitlab_url=os.getenv("GITLAB_URL", "https://gitlab.com"),
        gitlab_token=os.getenv("GITLAB_TOKEN", ""),
        project_path=os.getenv("DEMO_PROJECT", "sandbox/prodesgin"),
    )
    knowledge = knowledge_api or KnowledgeAPI(os.getenv("KNOWLEDGE_BASE_URL", "http://localhost:8004"))
    executor = executor or executor_from_env()
    ctx = FlowContext(
        llm=llm,
        approval_api=approval_api or ApprovalAPI(os.getenv("APPROVAL_GATEWAY_URL", "http://127.0.0.1:8001")),
        gitlab_api=gitlab_api,
        coder=coder,
        tester=tester,
        knowledge=knowledge,
        executor=executor,
        gate_api=gate_api,
        tools=tool_registry,
    )
    # WP8：工具注册表（岗位技能引用的工具必须已注册，执行期据此鉴权）

    # WP7：一个应用实例共用一个 checkpointer（PostgresSaver 持有连接，不能每次编译新建）；
    # 由 checkpointer 模块按 CHECKPOINTER 灰度决定 memory / postgres。
    checkpointer = build_checkpointer()
    # 把平台已有能力绑定为工具实现——"登记了但没接线"不算能力（课程图 62）
    def _tool_kb_search(query: str, source_prefix: str = "", limit: int = 5):
        hits = ctx.knowledge.search(query, limit=limit) if ctx.knowledge is not None else []
        if source_prefix:
            hits = [h for h in hits if str(h.get("source", "")).startswith(source_prefix)]
        return {"chunks": hits, "count": len(hits)}

    def _tool_rules_read():
        return rules_pack.stats()

    def _tool_approval_create(gate: str, context_snapshot: dict, task_ref: str = ""):
        payload = {
            "type": gate, "stage": f"Agent 工具调用 {gate}", "requester": "Agent@tool",
            "task_ref": task_ref, "context_snapshot": context_snapshot,
            "options": ["approve", "reject", "revise"],
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "escalation_policy": {"remind_after_hours": 8, "escalate_to": "team_lead", "timeout_action": "reject"},
        }
        created = ctx.approval_api.create_approval(payload)
        return {"approval_id": created.get("id", ""), "status": created.get("status", "")}

    def _tool_doc_write(structured: str, sources=None):
        return {"doc": structured, "sources": sources or [], "note": "文档写入由调用方落地（演练实现）"}

    def _tool_ambiguity_detect(text: str):
        result = ambiguity.summarize(text, sources=[])
        return {"findings": result["findings"], "questions": result["questions"],
                "defaults_to_confirm": result["defaults_to_confirm"]}

    def _tool_source_read(task_ref: str):
        return {"sources": [], "note": "材料读取依赖多模态归集（WP11）；当前从审批来源链取"}

    for tool_id, handler in {
        "kb.search": _tool_kb_search,
        "rules.read": _tool_rules_read,
        "approval.create": _tool_approval_create,
        "doc.write": _tool_doc_write,
        "ambiguity.detect": _tool_ambiguity_detect,
        "source.read": _tool_source_read,
    }.items():
        try:
            tool_registry.register_handler(tool_id, handler)
        except ToolError as exc:   # 工具未注册不阻断启动（但会在 /tools 与日志里显形）
            logger.warning("工具实现绑定失败：%s", exc)

    graphs = {
        SOP_PRODUCT: build_graph(ctx, checkpointer=checkpointer),
        SOP_DEV: build_dev_graph(ctx, checkpointer=checkpointer),
        SOP_TEST: build_test_graph(ctx, checkpointer=checkpointer),
        SOP_OPS: build_ops_graph(ctx, checkpointer=checkpointer),
    }

    app = FastAPI(
        title="agent-runtime",
        description="Agent 运行时：产品域 PRD+G1 / 研发域 方案+编码+MR+G3 / 测试域 独立用例+G4（设计文档 02）",
        version="0.3.0",
    )
    install_service_auth(app, "agent-runtime")
    app.state.ctx = ctx
    app.state.graphs = graphs
    app.state.tools = tool_registry

    @app.post("/runs")
    def start_run(body: RunIn) -> dict:
        if body.sop not in graphs:
            raise HTTPException(status_code=422, detail=f"未知 sop: {body.sop}（可选 {list(graphs)}）")
        graph = graphs[body.sop]
        # thread_id 需 URL 安全（task_ref 可能含 # / 等字符），做净化后用作幂等键
        thread_id = (
            body.task_ref.replace("#", "-").replace("/", "-")
            if body.task_ref
            else f"run_{uuid.uuid4().hex[:8]}"
        )
        config = _config(thread_id, body.sop)
        init = {
            "requirement": body.requirement_text,
            "title": body.title,
            "project_id": body.project_id,
            "path_with_namespace": body.path_with_namespace,
            "task_ref": body.task_ref,
            "sources": body.sources,   # WP1：澄清与 G1 审批都基于这份出处
            "logs": [],
        }
        if body.sop == SOP_DEV:
            init["prd_doc"] = body.prd_doc
        if body.sop == SOP_OPS:
            init["alert"] = body.alert
            init["context"] = body.context
            init["runbook"] = body.runbook
        # WP7：运行归档（谁触发、什么 sop、状态存哪里、用了多少 token/成本）
        if hasattr(ctx.llm, "reset_usage"):
            ctx.llm.reset_usage()
        runs.start_run(
            thread_id, sop=body.sop, trigger=body.trigger, tenant=body.tenant,
            task_ref=body.task_ref, role=body.role or _role_of(body.sop),
            checkpoint=checkpointer_kind(),
        )
        try:
            graph.invoke(init, config)
        except Exception as exc:
            if "GraphInterrupt" not in type(exc).__name__:
                runs.finish_run(thread_id, "failed", _usage_of(ctx), {"error": f"{type(exc).__name__}: {exc}"})
                raise
        body_out = _status(graph, thread_id, body.sop)
        _archive_progress(thread_id, body_out, ctx)
        return body_out

    @app.post("/runs/{thread_id}/resume")
    def resume_run(thread_id: str, body: ResumeIn) -> dict:
        if body.decision not in ("approve", "reject", "revise"):
            raise HTTPException(status_code=422, detail="decision 必须为 approve/reject/revise")
        # 从线程配置恢复 sop（兼容旧线程默认产品域）
        sop = SOP_PRODUCT
        graph = None
        for name, g in graphs.items():
            snap = g.get_state(_config(thread_id, name))
            if snap.values or snap.next:
                sop, graph = name, g
                break
        if graph is None:
            raise HTTPException(status_code=404, detail=f"线程 {thread_id} 不存在")
        config = _config(thread_id, sop)
        snap = graph.get_state(config)
        if snap.next and not any(g in list(snap.next) for g in ("gate_g1", "gate_g2", "gate_g3", "gate_g4", "gate_g5", "gate_g6")):
            raise HTTPException(status_code=409, detail="当前流程不在人审挂起状态")

        # 治理专项：不接受调用方自述的决策——必须能对上审批网关里的一条真实决策
        node = list(snap.next)[0] if snap.next else ""
        run_task_ref = str((snap.values or {}).get("task_ref", "") or "")
        try:
            evidence = gate_decisions.resolve(
                body.approval_id, body.decision, node, run_task_ref,
                fetch=lambda aid: ctx.approval_api.get_approval(aid),
            )
        except gate_decisions.GateDecisionError as exc:
            logger.warning("resume 被拒（%s）：thread=%s decision=%s approval=%s",
                           exc.reason, thread_id, body.decision, body.approval_id)
            raise HTTPException(exc.status_code, str(exc)) from exc

        # 修复 BUG-012（真正的根因）：/resume 此前**从不写运行归档**，
        # 于是被否决的运行在归档里永远停留在 waiting —— /runs 与 stats.success_rate 看不到否决。
        if hasattr(ctx.llm, "reset_usage"):
            ctx.llm.reset_usage()
        try:
            graph.invoke(Command(resume={"decision": body.decision, "comment": body.comment}), config)
        except Exception as exc:
            if "GraphInterrupt" not in type(exc).__name__:
                runs.finish_run(thread_id, "failed", _usage_of(ctx), {"error": f"{type(exc).__name__}: {exc}"})
                raise
        out = _status(graph, thread_id, sop)
        extra = {"decision": body.decision, "gate_decision_evidence": evidence}
        if body.approval_id:
            extra["approval_id"] = body.approval_id
        if body.decision == "revise":
            extra["revised"] = True
        _archive_progress(thread_id, out, ctx, extra=extra)
        return out

    @app.get("/runs/{thread_id}/state")
    def run_state(thread_id: str) -> dict:
        for name, g in graphs.items():
            snap = g.get_state(_config(thread_id, name))
            if snap.values or snap.next:
                interrupts = []
                for task in snap.tasks or []:
                    for i in task.interrupts or []:
                        interrupts.append(i.value)
                return {
                    "thread_id": thread_id,
                    "sop": name,
                    "next": list(snap.next or []),
                    "interrupts": interrupts,
                    "values": snap.values or {},
                }
        raise HTTPException(status_code=404, detail=f"线程 {thread_id} 不存在")

    @app.post("/runs/{thread_id}/wake")
    def wake_run(thread_id: str, body: "WakeIn") -> dict:
        """唤醒等待中的运行（WP7）：数据到了就继续，不必从头再来。

        对应课程图 48「7:30 拉数 → 数据没到先等待 → 8:10 到齐接着跑」。
        与 `/resume`（人审恢复）的区别：本端点是**数据/时间条件满足**后自动继续。
        """
        sop, graph = _locate_graph(graphs, thread_id)
        if graph is None:
            raise HTTPException(status_code=404, detail=f"线程 {thread_id} 不存在")
        config = _config(thread_id, sop)
        # 修复 BUG-007（CRITICAL）：/wake 是"数据到齐"的唤醒，只能用于数据等待。
        # 此前它无状态守卫，对**等待人审**的运行调用会把 gate 中断当作 data_ready 消费掉，
        # 而闸门路由把未知决策映射为否决 —— 等于用一次 HTTP 调用静默否决人审。
        snap = graph.get_state(config)
        pending = list(snap.next or [])
        gate_nodes = {"gate_g1", "gate_g2", "gate_g3", "gate_g4", "gate_g5", "gate_g6"}
        if any(node in gate_nodes for node in pending):
            raise HTTPException(
                status_code=409,
                detail=(f"线程正等待人审（next={pending}），不能用 /wake 唤醒；"
                        f"请调用 /runs/{thread_id}/resume 提交决策"),
            )
        if "await_data" not in pending:
            raise HTTPException(
                status_code=409,
                detail=f"线程当前不在数据等待状态（next={pending}），/wake 不适用",
            )
        if hasattr(ctx.llm, "reset_usage"):
            ctx.llm.reset_usage()
        try:
            graph.invoke(Command(resume={"decision": "data_ready", "data": body.data}), config)
        except Exception as exc:
            if "GraphInterrupt" not in type(exc).__name__:
                runs.finish_run(thread_id, "failed", _usage_of(ctx), {"error": f"{type(exc).__name__}: {exc}"})
                raise
        out = _status(graph, thread_id, sop)
        _archive_progress(thread_id, out, ctx)
        return out

    @app.get("/runs")
    def list_runs(tenant: str = "", role: str = "", status: str = "", limit: int = 50) -> dict:
        """运行归档（WP7）：可观测性与成本度量的数据源。"""
        return {
            "runs": runs.list_runs(tenant=tenant, role=role, status=status, limit=limit),
            "stats": runs.stats(tenant=tenant),
        }

    @app.get("/runs/{thread_id}/archive")
    def run_archive(thread_id: str) -> dict:
        item = runs.get_run(thread_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"无归档记录：{thread_id}")
        return item

    @app.get("/tools")
    def tools_info(role: str = "") -> dict:
        """工具注册表（WP8）：能力清单 + 写类/需人审清单。

        `?role=ops-duty` 时额外返回该岗位实际被允许的工具（取交集），
        用于核对"岗位边界与工具授权是否一致"（课程图 55：边界要与实际能力一致）。
        """
        info = tool_registry.summary()
        if role:
            spec = role_registry.load_role(role)
            info["role"] = role
            info["role_tools"] = tool_registry.allowed_for(spec.tool_ids)
            info["denied_for_role"] = sorted(t for t in spec.tool_ids if t not in tool_registry.ids)
        return info

    @app.post("/tools/invoke")
    def invoke_tool(body: "ToolInvokeIn") -> dict:
        """执行期鉴权下的工具调用（未授权/缺参数/未过人审都会被拒并留审计）。

        **鉴权专项**：`role` 与 `allow_ungated` 都来自请求体 = 调用方自述权限，等于没有权限。
        现在：
        - `allow_ungated`（绕过人审依据）默认**不认**，仅在 `TOOLS_ALLOW_UNGATED=1` 时放行；
          平台自己（SOP 图内部）不走这个 HTTP 入口，所以关掉它不影响正常流程。
        - `SERVICE_AUTH_MODE=token`（有管控的部署）下，`role` 也不再采信，除非显式
          `TOOLS_ALLOW_CLIENT_ROLE=1`——否则任何调用方都能自称 `dev-agent` 越权用工具。
        """
        allow_ungated = False
        if body.allow_ungated:
            if os.getenv("TOOLS_ALLOW_UNGATED", "0") == "1":
                allow_ungated = True
            else:
                raise HTTPException(status_code=403, detail={
                    "error": "allow_ungated 来自请求体，默认不予采信（人审依据不得由调用方自行豁免）",
                    "reason": "ungated_not_allowed",
                    "hint": "演练/内网需要时设 TOOLS_ALLOW_UNGATED=1",
                })
        role = body.role
        if role and os.getenv("SERVICE_AUTH_MODE", "off").lower() == "token" \
                and os.getenv("TOOLS_ALLOW_CLIENT_ROLE", "0") != "1":
            raise HTTPException(status_code=403, detail={
                "error": "受管控部署下不接受请求体自述的 role（调用方可自称任意岗位越权）",
                "reason": "role_not_trusted",
                "hint": "确需由调用方指定岗位时设 TOOLS_ALLOW_CLIENT_ROLE=1，并通过受信通道调用",
            })
        role_tools = None
        if role:
            role_spec = role_registry.load_role(role)
            role_tools = role_spec.tool_ids
        try:
            return tool_registry.invoke(
                body.tool, body.params, role=role, role_tools=role_tools,
                approval_ref=body.approval_ref, allow_ungated=allow_ungated,
            )
        except ToolDenied as exc:
            raise HTTPException(status_code=403, detail={"error": str(exc), "reason": exc.reason}) from exc
        except ToolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/tools/audit")
    def tools_audit(limit: int = 50) -> dict:
        """工具调用审计（含被拒记录）——课程图 59：边界要能查证。"""
        return {"count": len(tool_registry.audit_log), "entries": tool_registry.audit_log[-limit:]}

    @app.get("/roles")
    def roles_info(role: str = "", include_prompt: bool = False) -> dict:
        """岗位注册表（WP6）：三件套汇总 + 校验结果。

        `?role=ops-duty&include_prompt=true` 返回该岗位将注入 Agent 的完整提示词，
        便于核对"岗位边界到底写了什么"（课程图 55）。
        """
        if role:
            item = role_registry.load_role(role, known_tools=tool_registry.ids).to_dict(include_prompt=True)
            return {"role": item}
        roles, errors = [], []
        for path in sorted(role_registry.roles_dir().iterdir()):
            if not path.is_dir():
                continue
            try:
                roles.append(role_registry.load_role(path.name, known_tools=tool_registry.ids).to_dict())
            except role_registry.RoleError as exc:
                errors.append(str(exc))
        return {"dir": str(role_registry.roles_dir()), "count": len(roles), "roles": roles, "errors": errors}

    @app.get("/rules-pack")
    def rules_pack_info() -> dict:
        """项目规则包状态（WP4）：文件清单 + 指纹。

        指纹会随规则内容变化；prompt 里注入的正是这一版——用于核查"Agent 到底读没读规则"
        （计划 WP4 验收③：规则包必须被实际引用，而不是放在仓库里没人用）。
        """
        return rules_pack.stats()

    @app.get("/prompts")
    def prompts_info() -> dict:
        """当前 Prompt 版本（自进化应用后热更新，无需重启）。"""
        import hashlib

        info = active_versions()
        meta = {}
        for key, version in (info.get("prompts") or {}).items():
            content = get_prompt(key, "")
            meta[key] = {
                "version": version,
                "length": len(content),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
            }
        info["loaded_meta"] = meta
        return info

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": "agent-runtime",
            "version": "0.3.0",
            # WP7：如实说明"状态存哪里"——内存态不能声称支持重启恢复
            "checkpointer": checkpointer_describe(),
            # 治理姿态：宽松必须是看得见的（PLATFORM_ENV=dev 时这里会列出全部未开开关）
            "security": security_posture("agent-runtime"),
        }

    return app


app = create_app()
