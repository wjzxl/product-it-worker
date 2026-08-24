"""平台事件信封与发布器（契约见 contracts/events.md）。

注意：本文件与 gitlab-bridge/app/events.py、approval-gateway/app/events.py 内容保持一致；
出现第三个使用方时应提炼为 platform/libs/eventkit 共享包（TODO：提炼）。
"""
from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def new_event(
    event_type: str,
    payload: dict[str, Any],
    source: str,
    trace_id: str | None = None,
) -> dict:
    """构造标准事件信封。event_id 供消费方幂等去重。"""
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:16]}",
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "trace_id": trace_id or f"trace_{uuid.uuid4().hex[:16]}",
        "payload": payload,
    }


class Publisher(ABC):
    @abstractmethod
    def publish(self, event: dict) -> None:
        """发布事件。实现方必须保证不抛异常影响主流程。"""


class LogPublisher(Publisher):
    """默认桩：事件打到日志，保证本地开发可跑通。"""

    def publish(self, event: dict) -> None:
        logger.info(
            "[LogPublisher] %s\n%s",
            event["event_type"],
            json.dumps(event, ensure_ascii=False, indent=2),
        )


class FanoutPublisher(Publisher):
    """把事件 POST 到一组目标。单目标失败只记录错误，不抛异常、不回滚主流程。"""

    def __init__(self, targets: list[str], timeout: float = 5.0):
        self.targets = targets
        self._client = httpx.Client(timeout=timeout)

    def publish(self, event: dict) -> None:
        for target in self.targets:
            try:
                resp = self._client.post(target, json=event)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.error(
                    "[FanoutPublisher] 发布到 %s 失败: %s（event=%s）",
                    target, exc, event["event_id"],
                )


def publisher_from_env(publish_targets: list[str] | None = None) -> Publisher:
    return FanoutPublisher(publish_targets) if publish_targets else LogPublisher()
