"""SOP 优化提案：把挖掘报告交给 LLM 生成**结构化、可应用**的提案。

提案动作类型（`action.type`）：
- `trust_threshold`：调整信任参数（如 sample_ratio / l1_approve_rate / penalty_hours）
- `gate_sla`：调整某 Gate 的 SLA 小时数
- `prompt_version`：写入新 Prompt 版本并切换 active（`target`=Prompt 键，`value`=新 Prompt 全文）
- `knowledge_ingest`：把新知识/规范入库（`target`=来源前缀如 runbooks，`value`=Markdown 内容）
- `advice`：建议类（不改配置，仅登记改进项）
LLM 不可用时回退规则化提案（保证链路可用）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是流程改进专家。基于流程挖掘数据，输出**可执行**的 SOP 优化提案。\n"
    "严格输出 JSON：{\"proposals\": [{\"title\": \"...\", \"rationale\": \"...\", "
    "\"evidence\": \"引用具体指标\", \"action\": {\"type\": \"trust_threshold|gate_sla|advice\", "
    "\"target\": \"如 sample_ratio 或 GATE_PR_REVIEW\", \"value\": 0.3}, \"risk\": \"L2\"}]}\n"
    "规则：提案必须有数据依据；每次 1~3 条；只输出 JSON，不要解释。"
)

APPLIABLE_ACTIONS = ("trust_threshold", "gate_sla", "prompt_version", "knowledge_ingest")


def llm_from_env() -> Callable[[str], str] | None:
    base = os.getenv("LLM_BASE_URL", "")
    key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "qwen-max")
    if not (base and key):
        return None

    def chat(prompt: str) -> str:
        resp = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    return chat


def _parse(raw: str) -> list[dict] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    items = data.get("proposals") if isinstance(data, dict) else None
    return items if isinstance(items, list) else None


def rule_based(report: dict) -> list[dict]:
    """LLM 不可用时的规则化提案（与瓶颈识别同源，保证自动进化链路可用）。"""
    proposals: list[dict] = []
    for finding in report.get("bottlenecks", []):
        if finding["type"] == "high_rework":
            proposals.append(
                {
                    "title": f"降低 {finding['gate']} 退回率：前置预检",
                    "rationale": "退回率高说明上游产出未达审查要点，增加提交前自检可减少往返",
                    "evidence": finding["evidence"],
                    "action": {"type": "advice", "target": finding["gate"], "value": "precheck"},
                    "risk": "L2",
                }
            )
        elif finding["type"] == "slow_gate":
            proposals.append(
                {
                    "title": f"缩短 {finding['gate']} SLA 或提高抽样",
                    "rationale": "决策等待过长；对低风险项采用信任抽样可显著降低等待",
                    "evidence": finding["evidence"],
                    "action": {"type": "trust_threshold", "target": "sample_ratio", "value": 0.3},
                    "risk": "L2",
                }
            )
        elif finding["type"] == "timeout":
            proposals.append(
                {
                    "title": f"放宽 {finding['gate']} SLA",
                    "rationale": "超时自动处置率偏高，SLA 可能过紧",
                    "evidence": finding["evidence"],
                    "action": {"type": "gate_sla", "target": finding["gate"], "value": 48},
                    "risk": "L3",
                }
            )
    return proposals[:3]


def generate(report: dict, chat: Callable[[str], str] | None = None) -> list[dict]:
    """生成提案（LLM 优先，失败回退规则化）。"""
    chat = chat or llm_from_env()
    proposals: list[dict] = []
    if chat is not None:
        summary = {
            "gates": [
                {k: g.get(k) for k in ("gate", "domain", "requests", "decisions", "wait_p50_s", "wait_p95_s", "timeout_rate", "rework_rate")}
                for g in report.get("gates", [])
            ],
            "bottlenecks": report.get("bottlenecks", []),
            "top_rework_reasons": report.get("top_rework_reasons", [])[:5],
            "variants": report.get("variants", [])[:5],
            "cost_by_model": report.get("cost_by_model", []),
        }
        try:
            raw = chat(json.dumps(summary, ensure_ascii=False))
            proposals = _parse(raw) or []
        except httpx.HTTPError as exc:
            logger.warning("LLM 提案生成失败，回退规则化: %s", exc)
    if not proposals:
        proposals = rule_based(report)
    import re

    gate_re = re.compile(r"^GATE_[A-Z_]+$")
    trust_keys = {"window", "min_samples", "l1_approve_rate", "l2_approve_rate", "sample_ratio", "penalty_hours"}
    for i, p in enumerate(proposals, 1):
        p.setdefault("action", {"type": "advice", "target": "", "value": ""})
        p["id"] = f"prop-{i}"
        action = p["action"]
        # 校正：LLM 常把变体/路径当作 gate_sla 目标 → 降级为 advice（避免污染配置）
        if action.get("type") == "gate_sla" and not gate_re.match(str(action.get("target", ""))):
            p["action_original"] = dict(action)
            p["action"] = {"type": "advice", "target": action.get("target", ""),
                           "value": f"（原 gate_sla 目标非法，已降级）{action.get('value')}"}
            p["adjusted"] = "gate_sla 目标不是 GATE_*，已降级为 advice"
        if action.get("type") == "trust_threshold" and str(action.get("target", "")) not in trust_keys:
            p["action_original"] = dict(action)
            p["action"] = {"type": "advice", "target": action.get("target", ""),
                           "value": f"（原 trust_threshold 参数非法，已降级）{action.get('value')}"}
            p["adjusted"] = "trust_threshold 参数非法，已降级为 advice"
        p["appliable"] = p["action"].get("type") in APPLIABLE_ACTIONS
    return proposals
