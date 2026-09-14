"""SOP 配置中心读取：环境变量 > 租户覆盖 > sop-config.yaml > 内置默认。

- 平台基线：`platform/sop-config.yaml`；
- 产品线（租户）覆盖：`platform/sop-config.d/<tenant>.yaml`，只写差异项（多产品线推广时
  各产品线可独立设置信任阈值/Gate SLA，互不干扰）；
- 流程挖掘的"提案应用"通过改写配置文件生效（带 G7 审批留痕），mtime 热更新。
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
# 产品线（租户）覆盖目录：platform/sop-config.d/<tenant>.yaml
_overlay_env = os.getenv("SOP_OVERLAY_DIR", "")
_overlay_dir = Path(_overlay_env) if _overlay_env else (Path(__file__).resolve().parents[3] / "sop-config.d")
_cache: dict[str, tuple[dict[str, Any], tuple]] = {}

DEFAULT_TENANT = os.getenv("DEFAULT_TENANT", "default")


def config_path() -> Path | None:
    for c in _candidates:
        if c and c.exists():
            return c
    return None


def overlay_path(tenant: str | None) -> Path | None:
    """产品线覆盖文件路径；不存在返回 None（该产品线沿用平台基线）。"""
    if not tenant:
        return None
    path = _overlay_dir / f"{tenant}.yaml"
    return path if path.exists() else None


def _merge(base: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def load(force: bool = False, tenant: str | None = None) -> dict[str, Any]:
    """读取配置（缓存 + mtime 热更新：提案应用后无需重启即生效）。

    优先级：**租户覆盖文件 > 平台基线 sop-config.yaml > 内置默认**。
    缓存键含租户与其覆盖文件 mtime，某条产品线单独调参不影响其它产品线。
    """
    tenant = tenant or DEFAULT_TENANT
    path = config_path()
    opath = overlay_path(tenant)
    stamp = (
        path.stat().st_mtime if path else None,
        opath.stat().st_mtime if opath else None,
        str(opath) if opath else "",
    )
    cached = _cache.get(tenant)
    if cached is not None and not force and cached[1] == stamp:
        return cached[0]
    data = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for src in (path, opath):
        if not src:
            continue
        try:
            data = _merge(data, yaml.safe_load(src.read_text(encoding="utf-8")) or {})
        except Exception:
            logger.exception("读取 SOP 配置失败，跳过该文件: %s", src)
    _cache[tenant] = (data, stamp)
    return data


def trust_setting(name: str, env_name: str | None = None, cast=float, tenant: str | None = None):
    """信任参数：环境变量 > 租户覆盖 > 平台基线 > 默认。"""
    if env_name and os.getenv(env_name) not in (None, ""):
        return cast(os.getenv(env_name))
    cfg = load(tenant=tenant)
    return cast(cfg["trust"].get(name, DEFAULTS["trust"][name]))


def gate_sla_hours(gate_type: str, tenant: str | None = None) -> float:
    gates = load(tenant=tenant).get("gates") or {}
    entry = gates.get(gate_type) or {}
    return float(entry.get("sla_hours", 24))
