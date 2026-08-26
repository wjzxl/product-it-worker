"""Agent 运行时入口：产品域（PRD+G1）/ 研发域（方案+G2+编码+MR+G3+合入）/ 测试域（独立用例+G4 放行）。

Phase 0/1 编排方式：演示脚本驱动"启动 → 人审 → 恢复"；
Phase 2 起由工作流引擎（n8n/Temporal）消费 approval.decided 事件自动恢复。
"""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from .apis import ApprovalAPI, GitLabAPI
from .coder import Coder
from .llm import llm_from_env
from .sop import FlowContext, build_dev_graph, build_graph, build_ops_graph, build_test_graph
from .tester import TestEngine

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
    alert: dict = Field(default_factory=dict)   # 运维域：告警载荷
    context: str = ""       # 运维域：日志/指标上下文
    runbook: str = ""       # 运维域：Runbook 文本


class ResumeIn(BaseModel):
    decision: str = Field(min_length=1)  # approve / reject / revise
    comment: str = ""


def _config(thread_id: str, sop: str = SOP_PRODUCT) -> dict:
    return {"configurable": {"thread_id": thread_id, "sop": sop}}


def _status(graph, thread_id: str, sop: str) -> dict:
    snap = graph.get_state(_config(thread_id, sop))
    values = snap.values or {}
    next_nodes = list(snap.next or [])

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
        if gate == "gate_g6":
            body["postmortem"] = values.get("postmortem", "")
            body["execution"] = values.get("execution", "")
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
) -> FastAPI:
    llm = llm or llm_from_env()
    gitlab_api = gitlab_api or GitLabAPI(os.getenv("GITLAB_BRIDGE_URL", "http://localhost:8001"))
    coder = coder or Coder(
        llm=llm,
        gitlab_url=os.getenv("GITLAB_URL", "https://gitlab.com"),
        gitlab_token=os.getenv("GITLAB_TOKEN", ""),
        project_path=os.getenv("DEMO_PROJECT", "sandbox/prodesgin"),
    )
    tester = tester or TestEngine(
        llm=llm,
        coder=coder,
        gitlab_api=gitlab_api,
        gitlab_url=os.getenv("GITLAB_URL", "https://gitlab.com"),
        gitlab_token=os.getenv("GITLAB_TOKEN", ""),
        project_path=os.getenv("DEMO_PROJECT", "sandbox/prodesgin"),
    )
    ctx = FlowContext(
        llm=llm,
        approval_api=approval_api or ApprovalAPI(os.getenv("APPROVAL_GATEWAY_URL", "http://localhost:8000")),
        gitlab_api=gitlab_api,
        coder=coder,
        tester=tester,
    )
    graphs = {
        SOP_PRODUCT: build_graph(ctx),
        SOP_DEV: build_dev_graph(ctx),
        SOP_TEST: build_test_graph(ctx),
        SOP_OPS: build_ops_graph(ctx),
    }

    app = FastAPI(
        title="agent-runtime",
        description="Agent 运行时：产品域 PRD+G1 / 研发域 方案+编码+MR+G3 / 测试域 独立用例+G4（设计文档 02）",
        version="0.3.0",
    )
    app.state.ctx = ctx
    app.state.graphs = graphs

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
            "logs": [],
        }
        if body.sop == SOP_DEV:
            init["prd_doc"] = body.prd_doc
        if body.sop == SOP_OPS:
            init["alert"] = body.alert
            init["context"] = body.context
            init["runbook"] = body.runbook
        try:
            graph.invoke(init, config)
        except Exception as exc:
            if "GraphInterrupt" not in type(exc).__name__:
                raise
        return _status(graph, thread_id, body.sop)

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
        try:
            graph.invoke(Command(resume={"decision": body.decision, "comment": body.comment}), config)
        except Exception as exc:
            if "GraphInterrupt" not in type(exc).__name__:
                raise
        return _status(graph, thread_id, sop)

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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "agent-runtime", "version": "0.2.0"}

    return app


app = create_app()
