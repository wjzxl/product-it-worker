"""任务中心 API。

- POST /tasks             手工创建任务（Agent 运行时也可调用）
- POST /tasks/{id}/transition  状态流转（状态机校验）
- POST /tasks/{id}/events     追加时间线事件
- POST /events            平台事件接入（gitlab.* / approval.* → 任务状态联动，幂等收件箱）
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import alert_auth
from .service_auth import outbound_headers
from .db import get_db
from .events import new_event, publisher_from_env
from .models import EventInbox, Task, TaskEvent
from .schemas import EventEnvelope, PrometheusAlertsIn, TaskCreate, TaskOut, TimelineIn, TransitionIn
from .state import (DECISION_EXITS_FROM, DECISION_REQUIRED_TARGETS, TERMINAL,
                    validate_transition)

logger = logging.getLogger(__name__)
router = APIRouter()


def _publish_targets() -> list:
    import os

    raw = os.getenv("PUBLISH_TARGETS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


_publisher = publisher_from_env(_publish_targets() or None)


def current_tenant(x_tenant: str = Header(default="")) -> str:
    """多租户：请求头 X-Tenant 指定租户（缺省取 DEFAULT_TENANT）。"""
    import os

    return x_tenant or os.getenv("DEFAULT_TENANT", "default")


def _get_or_404(db: Session, task_id: str, tenant: Optional[str] = None,
                for_update: bool = False) -> Task:
    """按 id 取任务；给定 tenant 时做隔离（修复 BUG-006：此前只过滤 list）。

    `for_update=True` 对行加排他锁，用于状态流转这类"读状态 → 判合法性 → 写新状态"的路径。
    没有它，并发流转会各自读到旧状态、各自判合法、各自写成功（实测 30 并发 6 轮里 3 轮
    成功 2–15 次、时间线多出 1–14 条 status_changed）。SQLite 单写者，其方言忽略该子句。
    """
    stmt = select(Task).where(Task.id == task_id)
    if for_update:
        stmt = stmt.with_for_update()
    task = db.scalars(stmt).first()
    if task is None or (tenant is not None and (task.tenant or "default") != tenant):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"task {task_id} 不存在")
    return task


def _ensure_decision_present(db: Session, task: Task, target: str, detail: dict) -> None:
    """修复 BUG-005：进入 approved 必须能指明是哪次审批决策带来的。

    判定依据（任一即可，均要求非空）：
      - `detail.approval_id`（审批单号）
      - `detail.decided_by`（决策人）
    这样"审批通过"在任务时间线上是可追溯的事实，而不是一个自我声明的状态跳变。
    """
    if task.status != DECISION_EXITS_FROM or target not in DECISION_REQUIRED_TARGETS:
        return
    approval_id = str(detail.get("approval_id") or "").strip()
    decided_by = str(detail.get("decided_by") or "").strip()
    if not (approval_id or decided_by):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"任务处于 {DECISION_EXITS_FROM}，流转到 {target} 必须提供审批决策依据"
            f"（detail.approval_id 或 detail.decided_by）——没有决策不得恢复执行",
        )


def _record(db: Session, task: Task, event_type: str, by: str = "", detail: dict | None = None) -> TaskEvent:
    ev = TaskEvent(task_id=task.id, event_type=event_type, by=by, detail=detail or {})
    db.add(ev)
    return ev


def _maybe_auto_start_ops_sop(task: Task, alert: dict) -> None:
    """Prometheus 告警 → 自动启动运维域 SOP（AGENT_RUNTIME_URL 配置后生效）。"""
    import os

    import httpx

    url = os.getenv("AGENT_RUNTIME_URL", "")
    if not url:
        logger.warning("AGENT_RUNTIME_URL 未配置，跳过告警自动触发 task=%s", task.id)
        return
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    alert_body = {
        "id": task.external_ref,
        "title": labels.get("alertname", "告警"),
        "severity": labels.get("severity", "P2"),
        "source": labels.get("job") or labels.get("instance") or "prometheus",
        "message": annotations.get("summary") or annotations.get("description", ""),
    }
    context = f"{annotations.get('description', '')}\nlabels={labels}"
    try:
        resp = httpx.post(
            f"{url}/runs",
            json={
                "sop": "ops-domain/alert-to-postmortem",
                "requirement_text": alert_body["title"],
                "title": alert_body["title"],
                "task_ref": task.external_ref,
                "project_id": os.getenv("OPS_PROJECT_ID", ""),
                "alert": alert_body,
                "context": context,
                "runbook": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("告警自动触发运维域 SOP task=%s status=%s", task.id, resp.json().get("status"))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("告警自动触发失败 task=%s: %s", task.id, exc)


def _maybe_auto_start_dev_sop(task: Task, payload: dict) -> None:
    """Issue 带 auto-dev 标签 → 自动启动研发域 SOP（真实闭环触发点）。

    fire-and-forget：失败仅记录日志，不影响事件消费（Phase 1 试点用；
    Phase 2 由工作流引擎接管编排）。
    """
    import os

    import httpx

    labels = payload.get("labels") or []
    if "auto-dev" not in labels:
        return
    url = os.getenv("AGENT_RUNTIME_URL", "")
    if not url:
        logger.warning("AGENT_RUNTIME_URL 未配置，跳过 auto-dev 自动触发 task=%s", task.id)
        return
    try:
        resp = httpx.post(
            f"{url}/runs",
            json={
                "sop": "dev-domain/issue-to-merge",
                "requirement_text": payload.get("title", ""),
                "title": payload.get("title", ""),
                "task_ref": task.external_ref,
                "project_id": str(payload.get("project_id", "")),
                "prd_doc": payload.get("description", "") or payload.get("title", ""),
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("auto-dev 触发研发域 SOP task=%s status=%s", task.id, resp.json().get("status"))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("auto-dev 自动触发失败 task=%s: %s", task.id, exc)


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> Task:
    # 修复 BUG-015：空 external_ref 原样存 "" 会撞唯一索引，第二次建单直接 500。
    # 语义澄清：**没有外部引用就是 NULL**（唯一索引允许多个 NULL），而不是空字符串。
    ref = (payload.external_ref or "").strip() or None
    if ref:
        # 幂等查询必须**限定本租户**：否则 B 产品线用同一外部引用建单会拿回 A 产品线的任务
        # （跨租户读取，与 BUG-006 同源，由回归测试发现）。
        existing = db.scalar(select(Task).where(Task.external_ref == ref, Task.tenant == tenant))
        if existing:
            return existing  # 幂等：同一租户内同一外部引用只建一个任务
    task = Task(
        tenant=tenant,
        external_ref=ref,
        sop=payload.sop,
        params=payload.params,
        status="pending",
    )
    db.add(task)
    try:
        db.flush()
    except IntegrityError as exc:          # 并发下同一 external_ref 撞唯一索引
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"external_ref 冲突：{ref}") from exc
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
def list_tasks(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> list[Task]:
    stmt = select(Task).where(Task.tenant == tenant).order_by(Task.created_at.desc())
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    return list(db.scalars(stmt))


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> Task:
    return _get_or_404(db, task_id, tenant=tenant)


@router.post("/tasks/{task_id}/transition", response_model=TaskOut)
def transition(task_id: str, payload: TransitionIn, db: Session = Depends(get_db),
               tenant: str = Depends(current_tenant)) -> Task:
    task = _get_or_404(db, task_id, tenant=tenant, for_update=True)
    if not validate_transition(task.status, payload.to):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"非法状态流转 {task.status} -> {payload.to}（见 contracts/任务状态机）",
        )
    _ensure_decision_present(db, task, payload.to, payload.detail or {})   # 修复 BUG-005
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
def append_timeline(task_id: str, payload: TimelineIn, db: Session = Depends(get_db),
                    tenant: str = Depends(current_tenant)) -> Task:
    task = _get_or_404(db, task_id, tenant=tenant)
    _record(db, task, payload.event_type, by=payload.by, detail=payload.detail)
    db.commit()
    db.refresh(task)
    return task


@router.post("/alerts/prometheus", status_code=status.HTTP_200_OK)
async def prometheus_alerts(request: Request, payload: PrometheusAlertsIn,
                            db: Session = Depends(get_db)) -> dict:
    """Prometheus Alertmanager webhook：firing 告警 → 建任务 + 自动触发运维域 SOP。

    **鉴权（鉴权专项）**：此前该入口无任何鉴权，任何人都能凭空造告警拉起运维 SOP。
    现在要求 `X-Webhook-Token` 或 `X-Webhook-Signature: sha256=<hmac>`（见 `app/alert_auth.py`），
    未配置密钥时 fail-closed（503），不再"没配就是全开"。
    """
    raw = await request.body()
    try:
        channel = alert_auth.verify(dict(request.headers), raw)
    except alert_auth.AlertAuthError as exc:
        logger.warning("告警 webhook 被拒：%s（%s）", exc, exc.reason)
        raise HTTPException(exc.status_code, f"告警 webhook 鉴权失败：{exc}") from exc
    logger.info("告警 webhook 通过（%s）", channel)

    created: list[str] = []
    for alert in payload.alerts:
        if alert.get("status") not in (None, "firing"):
            continue
        labels = alert.get("labels") or {}
        name = labels.get("alertname", "alert")
        ref = f"alert-{labels.get('fingerprint') or name}-{int(time.time() * 1000) % 100000}"
        task = Task(
            external_ref=ref,
            sop="ops-domain/alert-to-postmortem",
            params={"alertname": name, "severity": labels.get("severity", "P2"), "labels": labels},
            status="pending",
        )
        db.add(task)
        db.flush()
        _record(db, task, "task.created", by="prometheus", detail={"alertname": name, "severity": labels.get("severity")})
        db.commit()
        db.refresh(task)
        _publisher.publish(new_event("task.created", {"task_id": task.id, "external_ref": ref}, "task-center"))
        _maybe_auto_start_ops_sop(task, alert)
        created.append(task.id)
    return {"status": "accepted", "tasks": created}


@router.post("/events", status_code=status.HTTP_200_OK)
def ingest_event(envelope: EventEnvelope, db: Session = Depends(get_db),
                 tenant: Optional[str] = Header(default=None, alias="X-Tenant")) -> dict:
    """平台事件接入（幂等：信封 event_id 去重，重复投递直接跳过）。

    **租户约束（修复 BUG-006）**：事件关联任务时，若带 `X-Tenant` 则限定该租户；
    未带时要求 external_ref **唯一命中**，命中多条（跨租户重名）则拒绝并记录，
    绝不"随便挑一条"改状态。
    """
    if db.get(EventInbox, envelope.event_id):
        return {"status": "duplicate_skipped", "event_id": envelope.event_id}
    db.add(EventInbox(event_id=envelope.event_id))
    db.commit()

    payload = envelope.payload
    etype = envelope.event_type

    if etype == "gitlab.issue.opened":
        ref = f"{payload.get('project_id')}#{payload.get('iid')}"
        # 修复 BUG-015：空 external_ref 会被唯一索引拦成 HTTP 500；
        # 这里显式判定并返回 422/409，让调用方拿到可处理的客户端错误。
        if not payload.get("project_id") or payload.get("iid") in (None, ""):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "external_ref 不能为空（需要 project_id 与 iid）")
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
        try:
            db.flush()
        except IntegrityError as exc:      # 并发下重复 external_ref
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, f"external_ref 冲突：{ref}") from exc
        _record(db, task, "task.created", by=envelope.source, detail={"trigger": etype})
        db.commit()
        db.refresh(task)
        _publisher.publish(
            new_event("task.created", {"task_id": task.id, "external_ref": ref}, "task-center", trace_id=envelope.trace_id)
        )
        _maybe_auto_start_dev_sop(task, payload)
        return {"status": "created", "task_id": task.id}

    task_ref = payload.get("task_ref", "")
    task = None
    if task_ref:
        stmt = select(Task).where(Task.external_ref == task_ref)
        if tenant:
            stmt = stmt.where(Task.tenant == tenant)
        matches = list(db.scalars(stmt))
        if len(matches) > 1:
            logger.warning("事件关联歧义：external_ref=%s 命中 %d 个任务（跨租户重名），"
                           "拒绝自动改状态，请带 X-Tenant 重投", task_ref, len(matches))
            return {"status": "ambiguous_ref", "matches": [t.id for t in matches]}
        task = matches[0] if matches else None

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
