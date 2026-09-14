"""Prompt 注册中心（版本化 + 热更新）。

读取顺序：`PROMPTS_DIR`（默认 platform/prompts）→ registry.yaml 的 active 版本 → `<key>.<version>.md`；
缺失或读取失败时回退调用方传入的内置默认 Prompt（保证服务可用）。

自进化闭环：process-mining 的 `prompt_version` 动作写入新版本并切换 active，
本模块按 mtime 感知变更，**无需重启**即生效。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "prompts"
_cache: dict | None = None
_cache_mtime: float | None = None


def prompts_dir() -> Path:
    return Path(os.getenv("PROMPTS_DIR", str(DEFAULT_DIR)))


def _registry_path() -> Path:
    return prompts_dir() / "registry.yaml"


def registry(force: bool = False) -> dict:
    """读取注册表（mtime 热更新）。"""
    global _cache, _cache_mtime
    path = _registry_path()
    mtime = path.stat().st_mtime if path.exists() else None
    if _cache is not None and not force and mtime == _cache_mtime:
        return _cache
    data: dict = {"version": 0, "prompts": {}}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            data = {"version": loaded.get("version", 0), "updated_at": loaded.get("updated_at"),
                    "prompts": loaded.get("prompts") or {}}
        except Exception:
            logger.exception("读取 Prompt 注册表失败：%s", path)
    _cache, _cache_mtime = data, mtime
    return data


def get_prompt(key: str, default: str = "") -> str:
    """取指定 Agent 的当前 active Prompt 内容。"""
    version = (registry().get("prompts") or {}).get(key)
    if not version:
        return default
    path = prompts_dir() / f"{key}.{version}.md"
    try:
        return path.read_text(encoding="utf-8").strip() or default
    except OSError:
        logger.warning("Prompt 文件缺失（%s），回退默认", path)
        return default


def active_versions() -> dict:
    reg = registry()
    return {
        "dir": str(prompts_dir()),
        "registry_version": reg.get("version"),
        "updated_at": reg.get("updated_at"),
        "prompts": reg.get("prompts") or {},
    }
