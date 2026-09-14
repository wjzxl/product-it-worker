"""流程挖掘与 SOP 自进化服务（Phase 3）。

闭环：
  挖掘（真实数据） → LLM 提案（结构化动作） → **G7 流程改进人审** → 应用（写 SOP 配置中心） → 登记改进项 → 效果度量

端点：
  GET  /mining/report                     挖掘报告（Gate 瓶颈/变体/成本）
  POST /mining/proposals                  生成提案（LLM，失败回退规则化）
  POST /mining/proposals/submit           提交提案 → 创建 G7 审批（approval-gateway）
  POST /mining/proposals/apply            应用已批准提案（校验审批状态 → 写配置 → 登记 GitLab 改进项）
  GET  /mining/applied                    已应用历史（配置中心的 version/updated_at）
  GET  /health
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .apply import apply_action, config_path, record_improvement
from .mining import mine
from .proposals import generate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

REPORTS_DIR = Path(os.getenv("REPORTS_DIR", str(Path(__file__).resolve().parents[3] / "reports")))
APPROVAL_GATEWAY_URL = os.getenv("APPROVAL_GATEWAY_URL", "http://localhost:8000")
GATE_TYPE = "GATE_PROCESS_IMPROVEMENT"


class SubmitIn(BaseModel):
    proposal: dict
    requester: str = "流程挖掘引擎"
    task_ref: str = "process-improvement"


class ApplyIn(BaseModel):
    proposal: dict
    approval_id: str = Field(min_length=1)


app = FastAPI(
    title="process-mining",
    description="流程挖掘与 SOP 自进化：瓶颈 → 提案 → G7 人审 → 配置应用（设计文档 05 Phase 3）",
    version="0.1.0",
)


@app.get("/mining/report")
def mining_report(days: int = 180) -> dict:
    return mine(days=days)


@app.post("/mining/proposals")
def make_proposals(days: int = 180) -> dict:
    report = mine(days=days)
    proposals = generate(report)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"mining-proposals-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"report_summary": {
        "gates": report["gates"], "bottlenecks": report["bottlenecks"], "variants": report["variants"],
        "cost_by_model": report["cost_by_model"],
    }, "proposals": proposals}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report_summary": {"bottlenecks": report["bottlenecks"], "gates": len(report["gates"])},
            "proposals": proposals, "saved_to": str(out)}


@app.post("/mining/proposals/submit")
def submit_proposal(body: SubmitIn) -> dict:
    """提交提案 → 创建 G7 审批（三要素：提案/依据/风险）。"""
    p = body.proposal
    payload = {
        "type": GATE_TYPE,
        "stage": "平台域 G7",
        "requester": body.requester,
        "task_ref": body.task_ref,
        "context_snapshot": {
            "artifact_ref": f"提案 {p.get('id', '')} {p.get('title', '')[:40]}",
            "diff_summary": f"SOP 优化提案：{p.get('title', '')}｜动作 {p.get('action')}",
            "evidence": [
                f"挖掘依据：{p.get('evidence', '')}",
                f"提案理由：{p.get('rationale', '')}",
                f"可自动应用：{p.get('appliable', False)}",
            ],
            "risk": {"score": 45, "level": p.get("risk", "L2"), "factors": ["流程配置变更", "可回滚"]},
            "self_review": "提案基于真实挖掘数据；应用将写入 SOP 配置中心（可回滚）",
        },
        "options": ["approve", "reject", "revise"],
        "deadline": (datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        "escalation_policy": {"remind_after_hours": 24, "escalate_to": "platform_lead", "timeout_action": "reject"},
    }
    # 默认 24h 期限（上面用当前时间占位，此处修正为 +24h）
    from datetime import timedelta

    payload["deadline"] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    try:
        resp = httpx.post(f"{APPROVAL_GATEWAY_URL.rstrip('/')}/approvals", json=payload, timeout=30)
        resp.raise_for_status()
        approval = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"创建 G7 审批失败: {exc}")
    logger.info("G7 提案已提交审批：%s（%s）", approval.get("id"), p.get("title"))
    return {"approval_id": approval.get("id"), "status": approval.get("status"), "proposal": p}


@app.post("/mining/proposals/apply")
def apply_proposal(body: ApplyIn) -> dict:
    """应用已批准的提案：校验 G7 审批状态 → 写 SOP 配置 → 登记 GitLab 改进项。"""
    try:
        apv = httpx.get(f"{APPROVAL_GATEWAY_URL.rstrip('/')}/approvals/{body.approval_id}", timeout=30).json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"读取审批失败: {exc}")
    if apv.get("status") != "approved":
        raise HTTPException(status_code=409, detail=f"提案尚未获批（当前状态 {apv.get('status')}），禁止应用")

    result = apply_action(body.proposal.get("action", {}))
    issue = record_improvement(body.proposal, result)
    return {
        "approval_id": body.approval_id,
        "approved_by": (apv.get("decisions") or [{}])[-1].get("decided_by", ""),
        "apply": result,
        "improvement_issue": issue,
    }


@app.get("/mining/applied")
def applied_history() -> dict:
    path = config_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"config": str(path), "version": data.get("version"), "updated_at": data.get("updated_at"),
            "trust": data.get("trust"), "gates": {k: v.get("sla_hours") for k, v in (data.get("gates") or {}).items()}}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "process-mining", "version": "0.1.0"}
