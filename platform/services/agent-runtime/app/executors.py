"""操作类变更执行器（运维域 D4/D6）：把"重启/扩容/回滚"等动作真正下发到执行目标。

模式（`OPS_EXECUTOR`）：
- `dryrun`（默认）：仅记录（无基础设施可对接时的安全默认）；
- `webhook`：POST 到外部运维 API（如公司内部运维平台 / 云函数 / ArgoCD Webhook），
  带 Bearer Token；真实可验证（测试用本地 mock 服务，生产填真实地址）。

诚实标注：云厂商 SDK 直连（如阿里云/腾讯云 OpenAPI）作为后续扩展点，接口已就位。
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class OperationExecutor:
    mode = "dryrun"

    def execute(self, action: str, target: str, params: dict | None = None) -> dict:
        raise NotImplementedError


class DryRunExecutor(OperationExecutor):
    """安全默认：只记录不执行。"""

    mode = "dryrun"

    def execute(self, action: str, target: str, params: dict | None = None) -> dict:
        detail = f"[dryrun] 计划执行 {action} → {target} {params or {}}"
        logger.info(detail)
        return {"ok": True, "mode": self.mode, "detail": detail}


class WebhookExecutor(OperationExecutor):
    """真实执行：POST 到外部运维 API。"""

    mode = "webhook"

    def __init__(self, url: str, token: str = "", timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.url = url
        self.token = token
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def execute(self, action: str, target: str, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"action": action, "target": target, "params": params or {}}
        try:
            resp = self._client.post(self.url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
            return {"ok": True, "mode": self.mode, "detail": f"{action} → {target} 已下发", "response": body}
        except httpx.HTTPError as exc:
            logger.error("操作执行失败 %s → %s: %s", action, target, exc)
            return {"ok": False, "mode": self.mode, "detail": f"{action} → {target} 下发失败: {exc}"}


def executor_from_env() -> OperationExecutor:
    mode = os.getenv("OPS_EXECUTOR", "dryrun").lower()
    url = os.getenv("OPS_EXECUTOR_URL", "")
    if mode == "webhook" and url:
        logger.info("操作执行器：webhook → %s", url)
        return WebhookExecutor(url, os.getenv("OPS_EXECUTOR_TOKEN", ""))
    if mode == "webhook" and not url:
        logger.warning("OPS_EXECUTOR=webhook 但未配置 OPS_EXECUTOR_URL，回退 dryrun")
    return DryRunExecutor()
