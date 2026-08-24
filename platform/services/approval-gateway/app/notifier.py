"""通知适配器：IM 卡片推送。Phase 0 提供日志桩，配好 FEISHU_WEBHOOK_URL 即启用飞书。"""
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, approval: Any) -> str:
        """推送审批卡片，返回外部消息 ID（用于回调关联）。"""


class LogNotifier(Notifier):
    """默认桩：把卡片内容打到日志，保证本地开发可跑通全流程。"""

    def send(self, approval: Any) -> str:
        card = build_card(approval)
        logger.info(
            "[LogNotifier] 审批卡片(未配置真实 IM)：approval_id=%s\n%s",
            approval.id,
            json.dumps(card, ensure_ascii=False, indent=2),
        )
        return f"log-{approval.id}"


class FeishuNotifier(Notifier):
    """飞书交互卡片（接口位）。

    TODO(T0.4): 实现飞书卡片 JSON 构造与回调绑定——
      1. 使用测试群自定义机器人 webhook 起步；
      2. 卡片按钮回调指向平台 /callbacks/im/card；
      3. 回调签名校验（timestamp + sign）防伪造。
    """

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, approval: Any) -> str:
        import httpx

        card = build_card(approval)
        resp = httpx.post(self.webhook_url, json={"msg_type": "interactive", "card": card}, timeout=10)
        resp.raise_for_status()
        return approval.id


def build_card(approval: Any) -> dict:
    """审批卡片内容（对齐 03 §3.2：摘要 + 三按钮）。"""
    ctx = approval.context_snapshot or {}
    risk = ctx.get("risk", {})
    return {
        "header": {"title": f"[待审批] {approval.stage}", "template": "red"},
        "elements": [
            {"tag": "div", "text": {"content": f"**做什么**\n{ctx.get('diff_summary', '')}"}},
            {
                "tag": "div",
                "text": {
                    "content": "\n".join(
                        [f"**依据**"] + [f"- {e}" for e in ctx.get("evidence", [])]
                    )
                },
            },
            {
                "tag": "div",
                "text": {
                    "content": (
                        f"**风险自评**：{risk.get('level', '?')}（{risk.get('score', '?')} 分）"
                        f"{' | ' + '、'.join(risk.get('factors', [])) if risk.get('factors') else ''}"
                    )
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"content": "批准"}, "type": "primary", "value": {"action": "approve"}},
                    {"tag": "button", "text": {"content": "退回"}, "type": "default", "value": {"action": "revise"}},
                    {"tag": "button", "text": {"content": "拒绝"}, "type": "danger", "value": {"action": "reject"}},
                ],
            },
        ],
    }


def get_notifier() -> Notifier:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "")
    return FeishuNotifier(webhook) if webhook else LogNotifier()
