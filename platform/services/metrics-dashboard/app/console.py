"""驾驶舱（WP10）：从"只读面板"升级为"决策台"。

对应课程图 67/68：
  - 驾驶舱显示**任务处在哪一步、完成多少、卡在哪里、什么需要自己决定**；
  - 驾驶舱的作用是**监督和介入**，不是再堆一个要求人逐项操作的后台；
  - 提醒要能**直接打开对应任务上下文**（点击直达供应商选择那类决策页）。

聚合三个来源（都是真实接口，不造数据）：
  1. 待审审批（approval-gateway `/approvals?status=pending`）——业务决策；
  2. 被闸门阻断的检查（`/eng-gates/checks` 中 blocked 且未反馈）——实施条件缺口；
  3. 等待中的运行（agent-runtime `/runs?status=waiting`）——等数据/等人。

每条待决策项都带：做了什么、依据、**为什么卡住**、可执行动作、以及**深链**。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

APPROVAL_URL = os.getenv("APPROVAL_GATEWAY_URL", "http://127.0.0.1:8001")
RUNTIME_URL = os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:8002")


def _get(url: str, params: Optional[dict] = None, timeout: float = 8.0) -> Any:
    """容错读取：某个上游不可用时返回 None，并在结果里标注（不编造数据）。"""
    try:
        resp = httpx.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("驾驶舱上游不可用：%s（%s: %s）", url, type(exc).__name__, exc)
        return None


def _approval_items(tenant: str) -> tuple[list[dict], Optional[str]]:
    data = _get(f"{APPROVAL_URL.rstrip('/')}/approvals", {"status_filter": "pending"})
    if data is None:
        return [], "审批网关不可用"
    items = []
    for apv in data:
        ctx = apv.get("context_snapshot") or {}
        sources = ctx.get("sources") or []
        items.append({
            "kind": "approval",
            "id": apv.get("id"),
            "title": f"{apv.get('type', '')}｜{apv.get('stage', '')}",
            "what": ctx.get("diff_summary", ""),
            "evidence": ctx.get("evidence", []),
            "source_count": len(sources),
            "risk": (ctx.get("risk") or {}).get("level", ""),
            "why_waiting": "等待人审决策",
            "actions": ["approve", "reject", "revise"],
            "deep_link": f"/approvals/{apv.get('id')}",
            "console_link": f"#approval-{apv.get('id')}",
            "task_ref": apv.get("task_ref", ""),
            "created_at": apv.get("created_at", ""),
            "trust_level": apv.get("trust_level", ""),
        })
    return items, None


def _gate_items(tenant: str) -> tuple[list[dict], Optional[str]]:
    data = _get(f"{APPROVAL_URL.rstrip('/')}/eng-gates/checks", {"limit": 100})
    if data is None:
        return [], "闸门历史不可用"
    items = []
    for chk in data:
        if not chk.get("blocked"):
            continue
        items.append({
            "kind": "gate_block",
            "id": chk.get("id"),
            "title": f"{chk.get('code', '')} 闸门阻断",
            "what": f"缺口：{', '.join(chk.get('gap_ids') or [])}",
            "evidence": [],
            "risk": "",
            "why_waiting": "实施条件未具备（补齐后可继续）",
            "actions": ["feedback_valid", "feedback_false_positive"],
            "deep_link": f"/eng-gates/checks?task_ref={chk.get('task_ref', '')}",
            "console_link": f"#gate-{chk.get('id')}",
            "task_ref": chk.get("task_ref", ""),
            "created_at": chk.get("checked_at", ""),
            "feedback": chk.get("feedback", ""),
        })
    return items, None


def _run_items() -> tuple[list[dict], Optional[str]]:
    data = _get(f"{RUNTIME_URL.rstrip('/')}/runs", {"status": "waiting", "limit": 50})
    if data is None:
        return [], "运行归档不可用"
    items = []
    for run in data.get("runs", []):
        items.append({
            "kind": "run_waiting",
            "id": run.get("thread_id"),
            "title": f"{run.get('role') or run.get('sop', '')}｜等待中",
            "what": run.get("waiting_reason", "") or "等待中",
            "evidence": [],
            "risk": "",
            "why_waiting": run.get("waiting_reason", "") or "等待数据或人审",
            # 等待中的运行都可以被"数据到齐"唤醒（唤醒动作语义即 resume with data）；
            # 这里不按关键词猜，避免因措辞不同而漏掉可执行动作
            "actions": ["wake"],
            "deep_link": f"{RUNTIME_URL.rstrip('/')}/runs/{run.get('thread_id')}/state",
            "console_link": f"#run-{run.get('thread_id')}",
            "task_ref": run.get("task_ref", ""),
            "created_at": run.get("started_at", ""),
            "waits": run.get("waits", 0),
        })
    return items, None


def build_queue(tenant: str = "default") -> dict:
    """待决策队列 + 上游可用性 + 真实计数。"""
    approvals, err_a = _approval_items(tenant)
    gates, err_g = _gate_items(tenant)
    runs_list, err_r = _run_items()
    items = approvals + gates + runs_list
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    errors = [e for e in (err_a, err_g, err_r) if e]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant,
        "queue": items,
        "counts": {
            "approvals": len(approvals),
            "gate_blocks": len(gates),
            "run_waits": len(runs_list),
            "total": len(items),
        },
        "upstream_errors": errors,
        "feedback_stats": _get(f"{APPROVAL_URL.rstrip('/')}/eng-gates/feedback/stats") or {},
    }


def submit_decision(approval_id: str, decision: str, comment: str, decided_by: str) -> dict:
    """从驾驶舱直接提交决策（仍走审批网关留痕，不绕过审计）。"""
    resp = httpx.post(
        f"{APPROVAL_URL.rstrip('/')}/approvals/{approval_id}/decision",
        json={"decision": decision, "comment": comment, "decided_by": decided_by},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def submit_feedback(check_id: str, verdict: str, note: str, by: str) -> dict:
    """误拦反馈（M1 决策 D3）：让人能一键标记"其实该放行"。"""
    resp = httpx.post(
        f"{APPROVAL_URL.rstrip('/')}/eng-gates/checks/{check_id}/feedback",
        json={"verdict": verdict, "note": note, "by": by},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()
