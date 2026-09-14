"""SOP 配置中心读取：环境变量 > sop-config.yaml > 内置默认。

流程挖掘的"提案应用"通过改写 sop-config.yaml 生效（带 G7 审批留痕）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "trust": {
        "window": 20,
        "min_samples": 10,
        "l1_approve_rate": 0.9,
        "l2_approve_rate": 0.8,
        "sample_ratio": 0.2,
        "penalty_hours": 24,
    },
    "gates": {},
}

_path_env = os.getenv("SOP_CONFIG_PATH", "")
_candidates = [
    Path(_path_env) if _path_env else None,
    Path(__file__).resolve().parents[3] / "sop-config.yaml",  # platform/sop-config.yaml
]
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None


def config_path() -> Path | None:
    for c in _candidates:
        if c and c.exists():
            return c
    return None


def load(force: bool = False) -> dict[str, Any]:
    """读取配置（缓存 + mtime 热更新：提案应用后无需重启即生效）。"""
    global _cache, _cache_mtime
    path = config_path()
    mtime = path.stat().st_mtime if path else None
    if _cache is not None and not force and mtime == _cache_mtime:
        return _cache
    _cache_mtime = mtime
    data = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for key, value in loaded.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key] = {**data[key], **value}
                else:
                    data[key] = value
        except Exception:
            logger.exception("读取 SOP 配置失败，使用默认值: %s", path)
    _cache = data
    return data


def trust_setting(name: str, env_name: str | None = None, cast=float):
    """信任参数：环境变量 > 配置文件 > 默认。"""
    if env_name and os.getenv(env_name) not in (None, ""):
        return cast(os.getenv(env_name))
    return cast(load()["trust"].get(name, DEFAULTS["trust"][name]))


def gate_sla_hours(gate_type: str) -> float:
    gates = load().get("gates") or {}
    entry = gates.get(gate_type) or {}
    return float(entry.get("sla_hours", 24))
