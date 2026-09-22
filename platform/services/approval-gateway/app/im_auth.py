"""IM 回调鉴权（修复 BUG-001）：飞书签名校验 + 可选主体鉴权。

背景：`/callbacks/im/card` 原本既无签名校验（源码留有 TODO）也无鉴权，任何能访问该端点的人
都能直接批准审批单——**人审闸门可被完全绕过**。这是本次全系统测试的 P0 发现。

飞书自定义机器人/卡片回调的签名方案（v2）：
    sign = sha256(timestamp + nonce + encrypt_key + raw_body)   # encrypt_key 即签名密钥
请求头：`X-Lark-Request-Timestamp`、`X-Lark-Request-Nonce`、`X-Lark-Signature`。

设计取舍（**安全默认**）：
- 未配置密钥时**拒绝**（503），而不是放行——本地演示显式设 `IM_CALLBACK_ALLOW_UNSIGNED=1`；
- 校验用 `hmac.compare_digest` 防时序侧信道；
- 时间戳超过容忍窗口（默认 300s）视为重放，拒绝；
- 校验失败只记必要信息（不落原始签名），避免日志泄露。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_SECONDS = 300


class ImAuthError(RuntimeError):
    """回调鉴权失败（携带 HTTP 状态与原因，便于路由层直接返回）。"""

    def __init__(self, message: str, status_code: int = 401, reason: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


def signing_key() -> str:
    """签名密钥：优先专用变量，其次复用平台 webhook 密钥。"""
    return os.getenv("IM_CALLBACK_SIGNING_KEY") or os.getenv("FEISHU_VERIFY_TOKEN") or ""


def allow_unsigned() -> bool:
    """仅用于本地演示/演练；默认关闭（未配置密钥即拒绝）。"""
    return os.getenv("IM_CALLBACK_ALLOW_UNSIGNED", "0") == "1"


def tolerance_seconds() -> int:
    try:
        return int(os.getenv("IM_CALLBACK_TOLERANCE_SECONDS", str(DEFAULT_TOLERANCE_SECONDS)))
    except ValueError:
        return DEFAULT_TOLERANCE_SECONDS


def compute_signature(timestamp: str, nonce: str, key: str, body: bytes) -> str:
    """按飞书规则计算签名（与官方 SDK 一致：sha256(timestamp+nonce+key+body)）。"""
    payload = f"{timestamp}{nonce}{key}".encode("utf-8") + body
    return hashlib.sha256(payload).hexdigest()


def verify(headers: dict, body: bytes, now: Optional[float] = None) -> None:
    """校验回调签名；失败抛 `ImAuthError`。

    `headers` 用普通 dict（key 大小写不敏感由调用方归一化，这里做小写匹配）。
    """
    key = signing_key()
    if not key:
        if allow_unsigned():
            logger.warning("IM 回调未配置签名密钥，且 IM_CALLBACK_ALLOW_UNSIGNED=1：跳过校验（仅限本地/演练）")
            return
        raise ImAuthError(
            "IM 回调未配置签名密钥（IM_CALLBACK_SIGNING_KEY / FEISHU_VERIFY_TOKEN）："
            "拒绝未鉴权回调；如确需本地演示请显式设置 IM_CALLBACK_ALLOW_UNSIGNED=1",
            status_code=503, reason="signing_key_missing",
        )

    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    timestamp = str(lowered.get("x-lark-request-timestamp") or "")
    nonce = str(lowered.get("x-lark-request-nonce") or "")
    signature = str(lowered.get("x-lark-signature") or "")
    if not (timestamp and nonce and signature):
        raise ImAuthError("缺少飞书签名请求头（X-Lark-Request-Timestamp/Nonce/Signature）",
                          status_code=401, reason="signature_headers_missing")

    try:
        ts = float(timestamp)
    except ValueError as exc:
        raise ImAuthError("签名时间戳非法", status_code=401, reason="bad_timestamp") from exc
    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance_seconds():
        raise ImAuthError(
            f"回调时间戳超出容忍窗口（{tolerance_seconds()}s），疑似重放",
            status_code=401, reason="stale_timestamp",
        )

    expected = compute_signature(timestamp, nonce, key, body)
    if not hmac.compare_digest(expected, signature):
        raise ImAuthError("回调签名校验失败", status_code=401, reason="bad_signature")
    logger.info("IM 回调签名校验通过（ts=%s）", timestamp)


def describe() -> dict:
    """自检信息（供 /health 如实报告回调鉴权是否开启）。"""
    key = signing_key()
    return {
        "signing_key_configured": bool(key),
        "allow_unsigned": allow_unsigned(),
        "tolerance_seconds": tolerance_seconds(),
        "protected": bool(key) and not allow_unsigned(),
    }
