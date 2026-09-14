"""Pydantic 模型：任务/时间线/平台事件入站。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    external_ref: str = ""
    sop: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class TransitionIn(BaseModel):
    to: str = Field(min_length=1)
    by: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class TimelineIn(BaseModel):
    event_type: str = Field(min_length=1)
    by: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class PrometheusAlertsIn(BaseModel):
    """Prometheus Alertmanager webhook 载荷（精简）。"""

    status: str = "firing"
    alerts: list[dict[str, Any]] = Field(default_factory=list)


class EventEnvelope(BaseModel):
    """平台事件信封（契约 contracts/events.md）。"""

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: Optional[datetime] = None
    source: str = ""
    trace_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    id: str
    event_type: str
    by: str
    detail: dict[str, Any]
    occurred_at: datetime

    model_config = {"from_attributes": True}


class TaskOut(BaseModel):
    id: str
    external_ref: str
    sop: str
    params: dict[str, Any]
    status: str
    current_step: str
    agent: str
    created_at: datetime
    updated_at: datetime
    events: list[EventOut] = []

    model_config = {"from_attributes": True}
