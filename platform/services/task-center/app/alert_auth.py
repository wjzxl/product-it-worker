"""告警 webhook 鉴权（鉴权专项）：Alertmanager 入站必须带共享密钥或 HMAC 签名。

背景（实测）：`POST /alerts/prometheus` 此前**没有任何鉴权**——任何能连上端口的人都能
凭空造告警 → 建运维任务 → 自动拉起运维域 SOP（`demo_alert.py` 里那个 `WEBHOOK_SECRET`
当时只给了 gitlab-bridge，task-center 根本没读，属于"看着像有鉴权"的假象）。

两种等效方式（任一满足即可）：
1. `X-Webhook-Token: <secret>`（Alertmanager 的 `http_config` 支持自定义头）；
2. `X-Webhook-Signature: sha256=<hmac_sha256(secret, raw_body)>`（推荐：载荷不可篡改）。

配置：`ALERT_WEBHOOK_SECRET`，未配置时回退 `WEBHOOK_SECRET`（与 GitLab 桥共用一把）。
**未配置任何密钥时拒绝（503）而不是放行**——"没配就是全开"正是本次要根治的问题。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import status

logger = logging.getLogger(__name__)


class AlertAuthError(Exception):
    """鉴权失败。`status_code` 直接给 FastAPI 抛出。"""

    def __init__(self, message: str, status_code: int, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


def signing_key() -> str:
    return os.getenv("ALERT_WEBHOOK_SECRET", "") or os.getenv("WEBHOOK_SECRET", "")


def expected_signature(raw_body: bytes, key: str) -> str:
    return hmac.new(key.encode(), raw_body, hashlib.sha256).hexdigest()


def verify(headers: dict, raw_body: bytes) -> str:
    """校验通过返回所用方式（token/signature），失败抛 `AlertAuthError`。"""
    key = signing_key()
    if not key:
        raise AlertAuthError(
            "告警 webhook 未配置密钥（ALERT_WEBHOOK_SECRET / WEBHOOK_SECRET），"
            "为避免匿名建任务已拒绝；请配置密钥并在 Alertmanager 侧带上 X-Webhook-Token 或签名",
            status.HTTP_503_SERVICE_UNAVAILABLE, reason="no-key",
        )

    lowered = {k.lower(): v for k, v in headers.items()}
    token = lowered.get("x-webhook-token", "")
    if token and hmac.compare_digest(token, key):
        return "token"

    signature = lowered.get("x-webhook-signature", "")
    if signature:
        provided = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
        if hmac.compare_digest(provided, expected_signature(raw_body, key)):
            return "signature"
        raise AlertAuthError("告警 webhook 签名不匹配", status.HTTP_401_UNAUTHORIZED, reason="bad-signature")

    raise AlertAuthError(
        "告警 webhook 缺少 X-Webhook-Token 或 X-Webhook-Signature",
        status.HTTP_401_UNAUTHORIZED, reason="no-credential",
    )


def describe() -> dict:
    return {"key_configured": bool(signing_key()),
            "accepts": ["X-Webhook-Token", "X-Webhook-Signature: sha256=<hmac>"]}
