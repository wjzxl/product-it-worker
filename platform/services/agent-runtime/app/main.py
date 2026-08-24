"""Agent 运行时入口：启动流程 / 人审后恢复 / 查询状态。

Phase 0 编排方式：演示脚本（scripts/）驱动"启动 → 人审 → 恢复"三步；
Phase 1 起由工作流引擎（n8n/Temporal）消费 approval.decided 事件自动恢复。
"""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from .apis import ApprovalAPI, GitLabAPI
from .llm import llm_from_env
from .sop import FlowContext, build_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class RunIn(BaseModel):
    requirement_text: str = Field(min_length=1)
    title: str = ""
    task_ref: str = ""
    project_id: str = "sandbox/prodesgin"
    path_with_namespace: str = ""


class ResumeIn(BaseModel):
    decision: str = Field(min_length=1)  # approve / reject / revise
    comment: str = ""


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _status(graph, thread_id: str) -> dict:
    snap = graph.get_state(_config(thread_id))
    values = snap.values or {}
    next_nodes = list(snap.next or [])

    if "gate_g1" in next_nodes:
        return {
            "thread_id": thread_id,
            "status": "waiting_approval",
            "approval_id": values.get("approval_id", ""),
            "prd_version": values.get("version"),
            "prd_doc": values.get("prd_doc", ""),
            "logs": values.get("logs", []),
        }
    if not next_nodes:
        if values.get("gate_decision") == "reject":
            return {
                "thread_id": thread_id,
                "status": "rejected",
                "comment": values.get("gate_comment", ""),
                "logs": values.get("logs", []),
            }
        return {
            "thread_id": thread_id,
            "status": "done",
            "issue": values.get("issue"),
            "prd_version": values.get("version"),
            "logs": values.get("logs", []),
        }
    return {"thread_id": thread_id, "status": "running", "next": next_nodes}


def create_app(
    approval_api=None,
    gitlab_api=None,
    llm=None,
) -> FastAPI:
    ctx = FlowContext(
        llm=llm or llm_from_env(),
        approval_api=approval_api or ApprovalAPI(os.getenv("APPROVAL_GATEWAY_URL", "http://localhost:8000")),
        gitlab_api=gitlab_api or GitLabAPI(os.getenv("GITLAB_BRIDGE_URL", "http://localhost:8001")),
    )
    graph = build_graph(ctx)

    app = FastAPI(
        title="agent-runtime",
        description="Agent 运行时：LangGraph 状态图 + G1 人审挂起/恢复（设计文档 02 §1）",
        version="0.1.0",
    )
    app.state.graph = graph
    app.state.ctx = ctx

    @app.post("/runs")
    def start_run(body: RunIn) -> dict:
        # thread_id 需 URL 安全（task_ref 可能含 # / 等字符），做净化后用作幂等键
        thread_id = (
            body.task_ref.replace("#", "-").replace("/", "-")
            if body.task_ref
            else f"run_{uuid.uuid4().hex[:8]}"
        )
        config = _config(thread_id)
        try:
            graph.invoke(
                {
                    "requirement": body.requirement_text,
                    "title": body.title,
                    "project_id": body.project_id,
                    "path_with_namespace": body.path_with_namespace,
                    "task_ref": body.task_ref,
                    "logs": [],
                },
                config,
            )
        except Exception as exc:  # GraphInterrupt 由引擎内部消化；此处兜底
            if "GraphInterrupt" not in type(exc).__name__:
                raise
        return _status(graph, thread_id)

    @app.post("/runs/{thread_id}/resume")
    def resume_run(thread_id: str, body: ResumeIn) -> dict:
        if body.decision not in ("approve", "reject", "revise"):
            raise HTTPException(status_code=422, detail="decision 必须为 approve/reject/revise")
        config = _config(thread_id)
        snap = graph.get_state(config)
        if snap.next and "gate_g1" not in list(snap.next):
            raise HTTPException(status_code=409, detail="当前流程不在人审挂起状态")
        try:
            graph.invoke(
                Command(resume={"decision": body.decision, "comment": body.comment}),
                config,
            )
        except Exception as exc:
            if "GraphInterrupt" not in type(exc).__name__:
                raise
        return _status(graph, thread_id)

    @app.get("/runs/{thread_id}/state")
    def run_state(thread_id: str) -> dict:
        snap = graph.get_state(_config(thread_id))
        interrupts = []
        for task in snap.tasks or []:
            for i in task.interrupts or []:
                interrupts.append(i.value)
        return {
            "thread_id": thread_id,
            "next": list(snap.next or []),
            "interrupts": interrupts,
            "values": snap.values or {},
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "agent-runtime", "version": "0.1.0"}

    return app


app = create_app()
