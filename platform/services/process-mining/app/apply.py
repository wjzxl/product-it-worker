"""提案应用：把已批准的提案写入 SOP 配置中心（并登记 GitLab 改进项）。"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "sop-config.yaml"
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def prompts_dir() -> Path:
    return Path(os.getenv("PROMPTS_DIR", str(DEFAULT_PROMPTS_DIR)))


SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_\-/]{0,40}$")

# 允许的信任参数与其取值校验
TRUST_KEYS = {
    "window": lambda v: isinstance(v, int) and 5 <= v <= 200,
    "min_samples": lambda v: isinstance(v, int) and 1 <= v <= 100,
    "l1_approve_rate": lambda v: isinstance(v, (int, float)) and 0.5 <= float(v) <= 1.0,
    "l2_approve_rate": lambda v: isinstance(v, (int, float)) and 0.3 <= float(v) <= 1.0,
    "sample_ratio": lambda v: isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0,
    "penalty_hours": lambda v: isinstance(v, (int, float)) and 1 <= float(v) <= 720,
}
GATE_RE = re.compile(r"^GATE_[A-Z_]+$")


def config_path() -> Path:
    return Path(os.getenv("SOP_CONFIG_PATH", str(DEFAULT_CONFIG)))


def _apply_prompt_version(target: str, value: object) -> dict:
    """Prompt 版本化：写入 <key>.v<N+1>.md 并切换 registry active 版本（agent-runtime 热更新生效）。"""
    text = str(value or "").strip()
    if not (50 <= len(text) <= 8000):
        return {"applied": False, "reason": f"Prompt 内容长度需在 50~8000 字符（当前 {len(text)}）"}
    path = prompts_dir() / "registry.yaml"
    if not path.exists():
        return {"applied": False, "reason": f"Prompt 注册表不存在: {path}"}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prompts = data.setdefault("prompts", {})
    if target not in prompts:
        return {"applied": False, "reason": f"未知 Prompt 键 {target!r}（可选 {sorted(prompts)}）"}
    before = str(prompts[target])
    try:
        n = int(before.lstrip("v")) + 1
    except ValueError:
        n = 2
    after = f"v{n}"
    (prompts_dir() / f"{target}.{after}.md").write_text(text + "\n", encoding="utf-8")
    prompts[target] = after
    data["version"] = int(data.get("version", 1)) + 1
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logger.info("Prompt 版本已更新：%s %s → %s", target, before, after)
    return {"applied": True, "kind": "prompt_version", "target": target, "before": before, "after": after,
            "file": str(prompts_dir() / f"{target}.{after}.md")}


def _apply_knowledge_ingest(target: str, value: object) -> dict:
    """知识库自动入库：把新规范/经验写入知识库（Agent 检索即可用，带来源）。"""
    text = str(value or "").strip()
    if not (20 <= len(text) <= 20000):
        return {"applied": False, "reason": f"知识内容长度需在 20~20000 字符（当前 {len(text)}）"}
    if not SOURCE_RE.match(target):
        return {"applied": False, "reason": f"非法来源前缀 {target!r}"}
    url = os.getenv("KNOWLEDGE_BASE_URL", "http://localhost:8004").rstrip("/")
    source = f"{target}/auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
    try:
        resp = httpx.post(f"{url}/documents", json={"source": source, "content": text}, timeout=120)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as exc:
        return {"applied": False, "reason": f"知识库入库失败: {exc}"}
    logger.info("知识已入库：%s（%s 块）", source, body.get("chunks"))
    return {"applied": True, "kind": "knowledge_ingest", "target": target, "source": source,
            "chunks": body.get("chunks")}


def apply_action(action: dict) -> dict:
    """应用单个动作：trust_threshold / gate_sla 写配置；advice 仅登记。"""
    kind = action.get("type")
    target = str(action.get("target", ""))
    value = action.get("value")
    path = config_path()

    if kind == "advice":
        return {"applied": False, "reason": "advice 类型仅登记改进项，不改配置", "config": str(path)}

    if kind == "prompt_version":
        return {**_apply_prompt_version(target, value), "config": str(prompts_dir() / "registry.yaml")}

    if kind == "knowledge_ingest":
        return {**_apply_knowledge_ingest(target, value), "config": f"kb:{os.getenv('KNOWLEDGE_BASE_URL', 'http://localhost:8004')}"}

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    before = None
    if kind == "trust_threshold":
        if target not in TRUST_KEYS:
            return {"applied": False, "reason": f"非法信任参数 {target}（允许：{sorted(TRUST_KEYS)}）", "config": str(path)}
        if not TRUST_KEYS[target](value):
            return {"applied": False, "reason": f"参数 {target} 取值越界：{value}", "config": str(path)}
        before = (data.get("trust") or {}).get(target)
        data.setdefault("trust", {})[target] = value
    elif kind == "gate_sla":
        if not GATE_RE.match(target):
            return {"applied": False, "reason": f"非法 Gate 名称 {target!r}（需形如 GATE_PR_REVIEW）", "config": str(path)}
        if not isinstance(value, (int, float)) or not (0 < float(value) <= 720):
            return {"applied": False, "reason": f"SLA 取值越界：{value}", "config": str(path)}
        gates = data.setdefault("gates", {})
        gate = gates.setdefault(target, {})
        before = gate.get("sla_hours")
        gate["sla_hours"] = value
    else:
        return {"applied": False, "reason": f"未知动作类型 {kind}", "config": str(path)}

    data["version"] = int(data.get("version", 1)) + 1
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logger.info("SOP 配置已更新：%s.%s %s → %s", kind, target, before, value)
    return {"applied": True, "config": str(path), "kind": kind, "target": target, "before": before, "after": value}


def record_improvement(proposal: dict, apply_result: dict, bridge_url: str | None = None, project_id: str | None = None) -> dict:
    """在 GitLab 登记改进项（真实留痕，可追踪效果）。"""
    url = bridge_url or os.getenv("GITLAB_BRIDGE_URL", "http://localhost:8001")
    project = project_id or os.getenv("PROJECT_ID") or os.getenv("DEMO_PROJECT_ID", "")
    if not project:
        return {"created": False, "reason": "未配置项目 ID"}
    try:
        resp = httpx.post(
            f"{url.rstrip('/')}/gitlab/issues",
            json={
                "project_id": project,
                "title": f"[流程改进] {proposal.get('title', '')}",
                "description": (
                    f"## 依据\n{proposal.get('evidence', '')}\n\n"
                    f"## 提案\n{proposal.get('rationale', '')}\n\n"
                    f"## 应用结果\n```\n{apply_result}\n```\n"
                ),
                "labels": ["process-improvement", "auto-mining"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        issue = resp.json()
        return {"created": True, "issue_iid": issue.get("iid"), "web_url": issue.get("web_url")}
    except httpx.HTTPError as exc:
        logger.warning("改进项登记失败: %s", exc)
        return {"created": False, "reason": str(exc)}
