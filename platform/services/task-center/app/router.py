"""任务中心 API。

- POST /tasks             手工创建任务（Agent 运行时也可调用）
- POST /tasks/{id}/transition  状态流转（状态机校验）
- POST /tasks/{id}/events     追加时间线事件
- POST /events            平台事件接入（gitlab.* / approval.* → 任务状态联动，幂等收件箱）
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .events import new_event, publisher_from_env
from .models import EventInbox, Task, TaskEvent
from .schemas import EventEnvelope, TaskCreate, TaskOut, TimelineIn, TransitionIn
from .state import TERMINAL, validate_transition

logger = logging.getLogger(__name__)
router = APIRouter()


def _publish_targets() -> list:
    import os

    raw = os.getenv("PUBLISH_TARGETS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


_publisher = publisher_from_env(_publish_targets() or None)


def _get_or_404(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"task {task_id} 不存在")
    return task


def _record(db: Session, task: Task, event_type: str, by: str = "", detail: dict | None = None) -> TaskEvent:
    ev = TaskEvent(task_id=task.id, event_type=event_type, by=by, detail=detail or {})
    db.add(ev)
    return ev


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> Task:
    if payload.external_ref:
        existing = db.scalar(select(Task).where(Task.external_ref == payload.external_ref))
        if existing:
            return existing  # 幂等：同一外部引用只建一个任务
    task = Task(
        external_ref=payload.external_ref,
        sop=payload.sop,
        params=payload.params,
        status="pending",
    )
    db.add(task)
    db.flush()
    _record(db, task, "task.created", by="task-center", detail={"sop": task.sop})
    db.commit()
    db.refresh(task)
    _publisher.publish(
        new_event(
            "task.created",
            {"task_id": task.id, "external_ref": task.external_ref, "sop": task.sop},
            "task-center",
        )
    )
    return task


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(status_filter: Optional[str] = None, db: Session = Depends(get_db)) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc())
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    return list(db.scalars(stmt))


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Session = Depends(get_db)) -> Task:
    return _get_or_404(db, task_id)


@router.post("/tasks/{task_id}/transition", response_model=TaskOut)
def transition(task_id: str, payload: TransitionIn, db: Session = Depends(get_db)) -> Task:
    task = _get_or_404(db, task_id)
    if not validate_transition(task.status, payload.to):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"非法状态流转 {task.status} -> {payload.to}（见 contracts/任务状态机）",
        )
    old_status = task.status
    task.status = payload.to
    task.current_step = payload.detail.get("step", task.current_step)
    task.agent = payload.detail.get("agent", task.agent)
    _record(db, task, "status_changed", by=payload.by, detail={"from": old_status, "to": payload.to, **payload.detail})
    db.commit()
    db.refresh(task)
    _publisher.publish(
        new_event(
            "task.status_changed",
            {"task_id": task.id, "from": task.status, "to": payload.to, "by": payload.by},
            "task-center",
        )
    )
    return task


@router.post("/tasks/{task_id}/events", response_model=TaskOut)
def append_timeline(task_id: str, payload: TimelineIn, db: Session = Depends(get_db)) -> Task:
    task = _get_or_404(db, task_id)
    _record(db, task, payload.event_type, by=payload.by, detail=payload.detail)
    db.commit()
    db.refresh(task)
    return task


@router.post("/events", status_code=status.HTTP_200_OK)
def ingest_event(envelope: EventEnvelope, db: Session = Depends(get_db)) -> dict:
    """平台事件接入（幂等：信封 event_id 去重，重复投递直接跳过）。"""
    if db.get(EventInbox, envelope.event_id):
        return {"status": "duplicate_skipped", "event_id": envelope.event_id}
    db.add(EventInbox(event_id=envelope.event_id))
    db.commit()

    payload = envelope.payload
    etype = envelope.event_type

    if etype == "gitlab.issue.opened":
        ref = f"{payload.get('project_id')}#{payload.get('iid')}"
        existing = db.scalar(select(Task).where(Task.external_ref == ref))
        if existing:
            logger.info("task 已存在 external_ref=%s，跳过", ref)
            return {"status": "exists", "task_id": existing.id}
        task = Task(
            external_ref=ref,
            sop="product-domain/prd-flow",
            params={
                "title": payload.get("title", ""),
                "description": payload.get("description", ""),
                "labels": payload.get("labels", []),
                "project_id": payload.get("project_id"),
                "path_with_namespace": payload.get("path_with_namespace", ""),
            },
            status="pending",
        )
        db.add(task)
        db.flush()
        _record(db, task, "task.created", by=envelope.source, detail={"trigger": etype})
        db.commit()
        db.refresh(task)
        _publisher.publish(
            new_event("task.created", {"task_id": task.id, "external_ref": ref}, "task-center", trace_id=envelope.trace_id)
        )
        return {"status": "created", "task_id": task.id}

    task_ref = payload.get("task_ref", "")
    task = db.scalar(select(Task).where(Task.external_ref == task_ref)) if task_ref else None

    if etype == "approval.requested" and task:
        if task.status == "running":
            task.status = "waiting_approval"
            _record(db, task, "approval.requested", by=payload.get("requester", ""), detail=payload)
        else:
            _record(db, task, "approval.requested", by=payload.get("requester", ""), detail=payload)
        db.commit()
        return {"status": "ok", "task_id": task.id}

    if etype == "approval.decided" and task:
        decision = payload.get("decision")
        mapping = {"approve": "running", "reject": "rejected", "revise": "running"}
        target = mapping.get(decision)
        if target and task.status == "waiting_approval":
            task.status = target
        _record(
            db, task, "approval.decided",
            by=payload.get("decided_by", ""),
            detail={"decision": decision, "comment": payload.get("comment", ""), "version": payload.get("version")},
        )
        db.commit()
        return {"status": "ok", "task_id": task.id}

    if task:
        _record(db, task, etype, by=envelope.source, detail=payload)
        db.commit()
        return {"status": "recorded", "task_id": task.id}

    logger.info("事件未关联任务，忽略: %s", etype)
    return {"status": "ignored"}
