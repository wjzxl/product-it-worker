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

from .algorithms import (
    alpha_miner,
    conformance_alpha,
    conformance_dfg,
    conformance_heuristic,
    dfg,
    extract_event_log,
    heuristic_miner,
    threshold_sweep,
)
from .apply import apply_action, config_path, record_improvement
from .mining import mine
from .proposals import generate
from .service_auth import install_service_auth, security_posture

from .service_auth import outbound_headers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

REPORTS_DIR = Path(os.getenv("REPORTS_DIR", str(Path(__file__).resolve().parents[3] / "reports")))
# 默认端口对齐 platform/scripts/run_local_stack.py（本机一键起栈的端口表）：审批网关 8001
APPROVAL_GATEWAY_URL = os.getenv("APPROVAL_GATEWAY_URL", "http://127.0.0.1:8001")
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
install_service_auth(app, "process-mining")


@app.get("/mining/event-log")
def event_log(limit_cases: int = 200) -> dict:
    """事件日志（任务时间线 + 审批决策）→ (case, activity, timestamp)。"""
    log = extract_event_log(os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform"))
    traces = dict(list(log["traces"].items())[:limit_cases])
    return {"cases": len(traces), "events_total": sum(len(t) for t in traces.values()), "traces": traces}


@app.get("/mining/dfg")
def dfg_view() -> dict:
    """DFG：活动频次 / 直接跟随边 / 开始结束活动。"""
    return dfg(extract_event_log(os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform")))


@app.get("/mining/heuristic")
def heuristic_view(threshold: float = 0.8, sop: Optional[str] = None) -> dict:
    """启发式 Miner：依赖度过滤后的因果网 + 长度-2 循环（可按域过滤）。"""
    return heuristic_miner(
        extract_event_log(os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform"), sop=sop),
        threshold=threshold,
    )


@app.get("/mining/alpha")
def alpha_view() -> dict:
    """α-Miner：因果/并发/选择关系 + Petri 网库所（含源/汇库所）。"""
    return alpha_miner(extract_event_log(os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform")))


@app.get("/mining/conformance")
def conformance_view(threshold: float = 0.8, algorithm: str = "dfg",
                     sop: Optional[str] = None, sweep: bool = False) -> dict:
    """一致性检查：dfg（留一法异常检测，默认）/ heuristic（边级）/ alpha（token replay）。

    - `sop`：按域拆分日志（异构日志必须按域检查才有意义）；
    - `sweep=true`：输出阈值敏感度扫描（0.3/0.5/0.8）。
    """
    log = extract_event_log(
        os.getenv("PLATFORM_DSN", "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform"),
        sop=sop,
    )
    if algorithm == "alpha":
        result = conformance_alpha(log, alpha_miner(log))
    elif algorithm == "heuristic":
        result = conformance_heuristic(log, heuristic_miner(log, threshold=threshold))
    else:  # dfg（默认）：留一法异常检测，适配异构日志
        result = conformance_dfg(log)
    result["sop"] = log.get("sop")
    result["sop_distribution"] = log.get("sop_distribution")
    if sweep:
        result["threshold_sweep"] = threshold_sweep(log)
    return result


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
    return {"status": "ok", "service": "process-mining", "version": "0.1.0",
            "security": security_posture("process-mining")}
