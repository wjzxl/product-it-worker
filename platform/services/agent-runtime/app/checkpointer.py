"""检查点持久化（WP7）：让"等待—恢复"成为可能。

背景（WP0 纠正过的注释）：P0 期间四处 `compile()` 都传 `MemorySaver()`——**状态在内存，
进程重启即丢**，因此"7:30 拉数 → 数据没到 → 8:10 到齐继续"这类跨时间的等待做不到。

本模块提供两种 checkpointer，由 `CHECKPOINTER` 灰度切换（计划 §7 的回滚开关）：
  - `memory`（默认）：行为与 P0 一致，适合单测与离线演示；
  - `postgres`：状态落 PG，**进程重启不丢**；DATABASE_URL 未配置时自动回退 memory 并告警。

PostgresSaver 的建表由 `setup()` 完成（首次调用幂等）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PG_DSN = "postgresql://platform:platform@127.0.0.1:5432/platform"


class CheckpointerError(RuntimeError):
    """checkpointer 初始化失败（配置错误或依赖缺失）。"""


def mode() -> str:
    return (os.getenv("CHECKPOINTER", "memory") or "memory").strip().lower()


def dsn() -> str:
    """PG DSN：优先 CHECKPOINTER_DSN，其次 DATABASE_URL，最后用平台默认库。"""
    return os.getenv("CHECKPOINTER_DSN") or os.getenv("DATABASE_URL") or DEFAULT_PG_DSN


def _normalize(dsn_value: str) -> str:
    """psycopg 需要不带 `+psycopg` 方言后缀的 DSN；这里统一一下。"""
    return dsn_value.replace("postgresql+psycopg://", "postgresql://")


def build(mode_override: Optional[str] = None):
    """构建 checkpointer。

    返回的对象同时提供 `kind` 属性（memory/postgres），便于在 `/health` 与运行归档里如实标注
    "本轮状态到底存在哪里"。
    """
    from langgraph.checkpoint.memory import MemorySaver

    want = (mode_override or mode()).lower()
    if want != "postgres":
        saver = MemorySaver()
        saver.kind = "memory"  # type: ignore[attr-defined]
        return saver

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
    except Exception as exc:  # 依赖缺失 → 回退并告警（不静默）
        logger.warning("CHECKPOINTER=postgres 但依赖不可用（%s），回退 memory", exc)
        saver = MemorySaver()
        saver.kind = "memory"  # type: ignore[attr-defined]
        return saver

    # PG 不可达/建表失败时**不能让服务起不来**（灰度开关必须可回退），
    # 但也不能静默：回退 memory 的同时显著告警，并在 /health 里如实反映。
    try:
        conn = Connection.connect(_normalize(dsn()), autocommit=True, prepare_threshold=0)
        saver = PostgresSaver(conn)
        saver.setup()  # 建表（幂等）
    except Exception as exc:
        logger.warning("PostgresSaver 初始化失败（%s: %s），回退 memory（状态将不跨重启）",
                       type(exc).__name__, exc)
        fallback = MemorySaver()
        fallback.kind = "memory"  # type: ignore[attr-defined]
        return fallback
    saver.kind = "postgres"  # type: ignore[attr-defined]
    logger.info("checkpointer=postgres（DSN=%s）", _normalize(dsn()).split("@")[-1])
    return saver


def describe(mode_override: Optional[str] = None) -> dict:
    """自检信息（供 /health）：如实说明状态存哪里。"""
    want = (mode_override or mode()).lower()
    return {
        "configured": want,
        "dsn": _normalize(dsn()).split("@")[-1] if want == "postgres" else "",
        "restart_safe": want == "postgres",
        "note": (
            "状态落 PG，进程重启可恢复"
            if want == "postgres"
            else "状态在内存，进程重启即丢（等待/恢复能力不可用）"
        ),
    }
