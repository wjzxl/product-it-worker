"""人审决策核验（治理专项）：`/resume` 的决策必须能对上一条真实的审批决策。

背景（实测）：`POST /runs/{thread_id}/resume` 只要线程正挂在 Gate 上，就接受**任何调用方
自述的** `decision=approve` 直接恢复执行——人审闸门在 HTTP 层是可绕过的（`/wake` 早已加
守卫，`/resume` 没有）。这与平台的核心承诺（"AI 执行、人在闸门决策"）直接冲突。

核验规则（带 `approval_id` 时**总是**核验，与模式无关）：
  ① 审批单存在（404 → 409，不泄露别的租户单据是否存在）；
  ② 审批单状态与请求的 decision 一致：approve↔approved / reject↔rejected / revise↔revised；
  ③ 审批单的 `task_ref` 与本运行一致（两边都非空时比较）；
  ④ 审批单的 `type` 与当前挂起的 Gate 节点一致（如 gate_g3 ↔ GATE_PR_REVIEW）。

模式 `GATE_DECISION_VERIFY`：
| 值 | 行为 |
|---|---|
| `off` | 不核验（仅用于本地调试，会在日志中显式告警） |
| `warn`（本地默认） | 带了 approval_id 就核验并不符即拒；没带则放行 + WARNING |
| `block`（生产默认） | 必须带 approval_id 且核验通过，否则 409 |

默认值随 `SERVICE_AUTH_MODE` 走：`SERVICE_AUTH_MODE=token` ⇒ `block`，否则 `warn`。
"""
from __future__ import annotations

import logging
import os

from fastapi import status

logger = logging.getLogger(__name__)

# 挂起节点 → 该节点应出示的 Gate 类型（与 app/sop.py 里创建的审批 type 一致）
GATE_TYPE_BY_NODE = {
    "gate_g1": "GATE_REQUIREMENT_REVIEW",
    "gate_g2": "GATE_TECH_DESIGN_REVIEW",
    "gate_g3": "GATE_PR_REVIEW",
    "gate_g4": "GATE_RELEASE_DECISION",
    "gate_g5": "GATE_CHANGE_APPROVAL",
    "gate_g6": "GATE_POSTMORTEM_REVIEW",
}

DECISION_TO_STATUS = {"approve": "approved", "reject": "rejected", "revise": "revised"}


class GateDecisionError(Exception):
    def __init__(self, message: str, status_code: int = status.HTTP_409_CONFLICT, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


def mode() -> str:
    explicit = os.getenv("GATE_DECISION_VERIFY", "").strip().lower()
    if explicit in ("off", "warn", "block"):
        return explicit
    return "block" if os.getenv("SERVICE_AUTH_MODE", "off").lower() == "token" else "warn"


def verify(approval: dict, *, approval_id: str, decision: str, node: str = "", task_ref: str = "") -> None:
    """核对一条审批单能否支撑本次 resume。不符抛 `GateDecisionError`。"""
    expected_status = DECISION_TO_STATUS.get(decision)
    if expected_status is None:
        raise GateDecisionError(f"未知决策 {decision!r}", status.HTTP_422_UNPROCESSABLE_ENTITY, "bad-decision")

    actual = str(approval.get("status", ""))
    if actual != expected_status:
        raise GateDecisionError(
            f"审批 {approval_id} 的实际状态是 {actual!r}，与请求的 {decision!r}（应为 {expected_status}）不一致——"
            "不接受自述决策，请以审批台里的真实决策为准",
            reason="status-mismatch",
        )

    apv_ref = str(approval.get("task_ref") or "")
    if task_ref and apv_ref and apv_ref != task_ref:
        raise GateDecisionError(
            f"审批 {approval_id} 属于 {apv_ref!r}，与本次运行 {task_ref!r} 不是同一件事",
            reason="task-ref-mismatch",
        )

    if node:
        expected_type = GATE_TYPE_BY_NODE.get(node)
        actual_type = str(approval.get("type") or "")
        if expected_type and actual_type and actual_type != expected_type:
            raise GateDecisionError(
                f"审批 {approval_id} 是 {actual_type!r}，与当前挂起节点 {node}（应为 {expected_type}）不匹配",
                reason="gate-type-mismatch",
            )


def resolve(approval_id: str, decision: str, node: str, task_ref: str, fetch) -> str:
    """按模式决定是否放行。`fetch(approval_id)` 返回审批单 dict，失败抛 `GateDecisionError`。

    返回放行依据（'verified' / 'warn:no-approval-id' / 'off'），便于写进归档。
    """
    current = mode()
    if current == "off":
        if approval_id:
            logger.warning("GATE_DECISION_VERIFY=off：跳过对 %s 的人审决策核验（生产请勿如此）", approval_id)
        return "off"

    if not approval_id:
        if current == "block":
            raise GateDecisionError(
                "恢复人审挂起必须带 approval_id（审批台的决策编号）——不允许自述决策恢复执行",
                reason="approval-id-required",
            )
        logger.warning("resume 未带 approval_id，warn 模式放行（生产模式为 block）：node=%s task_ref=%s",
                       node, task_ref)
        return "warn:no-approval-id"

    try:
        approval = fetch(approval_id)
    except Exception as exc:  # noqa: BLE001 —— 网关不可用/404 都按"无法证明有决策"处理
        raise GateDecisionError(
            f"无法从审批网关取到审批 {approval_id} 的决策依据（{type(exc).__name__}: {exc}）——"
            "拿不到证据就不恢复执行",
            reason="approval-unreachable",
        ) from exc

    verify(approval, approval_id=approval_id, decision=decision, node=node, task_ref=task_ref)
    logger.info("人审决策核验通过：approval=%s decision=%s node=%s", approval_id, decision, node)
    return "verified"
