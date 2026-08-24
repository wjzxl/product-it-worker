"""通知适配器：IM 审批卡片推送。

Phase 0 通道策略：
- LogNotifier：默认桩，本地开发跑通全流程；
- FeishuNotifier：飞书自定义机器人 webhook 真实推送（interactive 卡片）。
  自定义机器人只能"推送"，按钮回调需要正式应用（callback 模式）；
  Phase 0 采用 URL 模式：卡片按钮跳转 Web 审批台（APPROVAL_WEB_BASE_URL）。
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, approval: Any) -> str:
        """推送审批卡片，返回消息 ID（失败返回空串，不阻断审批主流程）。"""


class LogNotifier(Notifier):
    def send(self, approval: Any) -> str:
        card = build_card(approval, os.getenv("APPROVAL_WEB_BASE_URL", ""))
        logger.info(
            "[LogNotifier] 审批卡片(未配置真实 IM)：approval_id=%s\n%s",
            approval.id,
            json.dumps(card, ensure_ascii=False, indent=2),
        )
        return f"log-{approval.id}"


class FeishuNotifier(Notifier):
    """飞书自定义机器人（webhook 推送）。"""

    def __init__(self, webhook_url: str, client: httpx.Client | None = None):
        self.webhook_url = webhook_url
        self._client = client

    def send(self, approval: Any) -> str:
        card = build_card(approval, os.getenv("APPROVAL_WEB_BASE_URL", ""))
        try:
            client = self._client or httpx.Client(timeout=10)
            resp = client.post(self.webhook_url, json={"msg_type": "interactive", "card": card})
            resp.raise_for_status()
            return approval.id
        except httpx.HTTPError as exc:
            logger.error("[FeishuNotifier] 推送失败 approval=%s: %s", approval.id, exc)
            return ""


def build_card(approval: Any, web_base_url: str = "") -> dict:
    """构造飞书 interactive 卡片（对齐 03 §3.2：摘要 + 三要素 + 处理入口）。"""
    ctx = approval.context_snapshot or {}
    risk = ctx.get("risk") or {}
    factors = "、".join(risk.get("factors") or [])

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**做什么**\n{ctx.get('diff_summary', '')}"}},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**依据**\n" + "\n".join(f"- {e}" for e in ctx.get("evidence", [])),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**风险自评**：{risk.get('level', '?')}（{risk.get('score', '?')} 分）"
                + (f"，{factors}" if factors else ""),
            },
        },
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"审批单 {approval.id} · 截止 {approval.deadline.isoformat()}"}
            ],
        },
    ]
    if web_base_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "去审批台处理"},
                        "type": "primary",
                        "url": f"{web_base_url.rstrip('/')}/?approval_id={approval.id}",
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"[待审批] {approval.stage}"},
            "template": "red",
        },
        "elements": elements,
    }


def get_notifier() -> Notifier:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "")
    return FeishuNotifier(webhook) if webhook else LogNotifier()
