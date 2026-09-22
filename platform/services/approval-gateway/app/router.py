"""审批网关 API。

- 工作流引擎在 Gate 处调用 POST /approvals 创建审批（三要素校验）；
- 人在 Web 审批台或 IM 卡片上做决策；
- 决策结果写审计留痕，并发布 approval.decided 事件（Phase 0 事件走回调/轮询）。
"""
import json
import logging
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException, Request,
                     Response, UploadFile, status)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import eng_gates, evidence, gate_rules, im_auth
from .db import get_db
from .events import new_event, publisher_from_env
from .models import ApprovalRequest, Decision, utcnow
from .notifier import get_notifier
from .policies import is_overdue
from .auth import approver_roles, auth_summary, current_principal, require_role
from .trust import all_trust, apply_penalty, compute_trust, should_auto_approve, tenant_summary
from .schemas import (
    ApprovalRequestCreate,
    ApprovalRequestOut,
    DecisionIn,
    DecisionOut,
    GateCheckIn,
    GateCheckOut,
    GateFeedbackIn,
    ImCardCallback,
)

logger = logging.getLogger(__name__)
router = APIRouter()

STATUS_TERMINAL = {"approved", "rejected"}


def _publish_targets() -> list:
    import os

    raw = os.getenv("PUBLISH_TARGETS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


_publisher = publisher_from_env(_publish_targets() or None)


def current_tenant(x_tenant: str = Header(default="")) -> str:
    """多租户：请求头 X-Tenant 指定租户（缺省取 DEFAULT_TENANT）。"""
    import os

    return x_tenant or os.getenv("DEFAULT_TENANT", "default")


def _check_decision_allowed(req: ApprovalRequest, decision: str) -> None:
    """修复 BUG-013：`options` 声明了什么就只允许什么（契约必须被执行）。

    此前 options 被存储但从不校验：声明 `["approve","reject"]` 的单据仍可被 revise，
    属契约漂移——闸门声明了可用动作，就应该真的只允许这些动作。
    """
    options = list(req.options or [])
    if options and decision not in options:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"该审批单只允许 {options}，收到 {decision!r}",
        )


def _get_or_404(
    db: Session,
    approval_id: str,
    tenant: Optional[str] = None,
    for_update: bool = False,
) -> ApprovalRequest:
    """取审批单（可选租户隔离）。`for_update=True` 时对行加排他锁，用于决策类写路径。

    **为什么需要 `SELECT … FOR UPDATE`**（专项 CONC-001）：决策路径是"读状态 → 判 pending →
    写决策"，PG 默认 READ COMMITTED 下两个并发请求会同时读到 pending，各自插入一条决策并
    都返回 200（实测 30 并发 8 轮中 6 轮发生）。加锁后第二个事务会阻塞到第一个提交，重新读到
    终态后由 `_check_pending` 返回 409。SQLite 单写者，`FOR UPDATE` 由其方言静默忽略，
    单元测试行为不变。
    """
    stmt = select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
    if for_update:
        stmt = stmt.with_for_update()
    req = db.scalars(stmt).first()
    if req is None or (tenant is not None and req.tenant != tenant):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"approval {approval_id} 不存在")
    return req


def _check_pending(req: ApprovalRequest) -> None:
    if req.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"审批已处理（当前状态 {req.status}）")
    if is_overdue(req.deadline):
        raise HTTPException(status.HTTP_410_GONE, "审批已超时，请按超时策略重新发起")


@router.post("/approvals", response_model=ApprovalRequestOut, status_code=status.HTTP_201_CREATED)
def create_approval(
    payload: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> ApprovalRequest:
    """工作流到达 Gate 时调用。三要素校验已在 schema 层强制。

    `auto_gate=True` 时按信任等级决定：L1 自动放行 / L2 抽样命中自动放行 / L3 人工审批。
    自动放行一律留痕（决策人 `system:trust`），并发布 approval.decided 事件。
    """
    req = ApprovalRequest(
        tenant=tenant,
        type=payload.type,
        stage=payload.stage,
        requester=payload.requester,
        task_ref=payload.task_ref,
        context_snapshot=payload.context_snapshot.model_dump(mode="json"),
        # WP1：来源链冗余到独立列，便于按来源/任务反查（空列表=存量数据，不算缺失）
        sources=[s.model_dump(mode="json") for s in payload.context_snapshot.sources],
        options=[o for o in payload.options],
        deadline=payload.deadline,
        escalation_policy=payload.escalation_policy.model_dump(),
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # 推送审批卡片（Web/IM 双通道；Phase 0 日志桩）
    get_notifier().send(req)
    # 发布 approval.requested 事件（契约 contracts/events.md），驱动工作流扇出
    _publisher.publish(
        new_event(
            "approval.requested",
            {
                "approval_id": req.id,
                "gate": req.type,
                "stage": req.stage,
                "task_ref": req.task_ref,
                "requester": req.requester,
                "deadline": req.deadline.isoformat(),
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    logger.info("approval.requested id=%s stage=%s", req.id, req.stage)

    trust_info = compute_trust(db, req.type, tenant=req.tenant)
    auto_decision = "pending"
    if payload.auto_gate:
        allowed, why = should_auto_approve(trust_info, req.id)
        if allowed:
            decision = Decision(
                request_id=req.id,
                decision="approve",
                decided_by="system:trust",
                comment=f"信任自动放行：{why}（{trust_info['reason']}）",
            )
            db.add(decision)
            req.status = "approved"
            db.commit()
            db.refresh(req)
            auto_decision = "approved"
            _publisher.publish(
                new_event(
                    "approval.decided",
                    {
                        "approval_id": req.id,
                        "task_ref": req.task_ref,
                        "decision": "approve",
                        "decided_by": "system:trust",
                        "comment": decision.comment,
                        "channel": "trust",
                        "version": req.version,
                    },
                    "approval-gateway",
                )
            )
            logger.info("信任自动放行 id=%s level=%s why=%s", req.id, trust_info["level"], why)
        else:
            auto_decision = "sampled_review" if trust_info["level"] == "L2" else "pending"

    out = ApprovalRequestOut.model_validate(req)
    out.trust_level = trust_info["level"]
    out.trust_reason = trust_info["reason"]
    out.auto_decision = auto_decision
    return out


@router.get("/approvals", response_model=list[ApprovalRequestOut])
def list_approvals(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant == tenant).order_by(ApprovalRequest.created_at.desc())
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    return list(db.scalars(stmt))


@router.get("/approvals/{approval_id}", response_model=ApprovalRequestOut)
def get_approval(approval_id: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> ApprovalRequest:
    return _get_or_404(db, approval_id, tenant=tenant)


@router.get("/requirements/{task_ref}/provenance")
def requirement_provenance(
    task_ref: str,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> dict:
    """需求来源链反查（WP1）：产物/task_ref → 当初是谁、在哪句话/哪个单元格里提出的。

    对应课程图 11「每条需求都带出处」与图 33「证据要能复验」：
    争论某个字段/口径为什么存在时，从这里回查，而不是翻聊天记录。

    返回按 Gate 时间线聚合的来源条目（含未确认的候选），并给出**仍缺出处的 Gate**，
    便于工程闸门 E1 直接消费。
    """
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.task_ref == task_ref, ApprovalRequest.tenant == tenant)
        .order_by(ApprovalRequest.created_at.asc())
    )
    rows = list(db.scalars(stmt))
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"任务 {task_ref} 在本产品线无审批记录")

    entries: list[dict] = []
    seen: set[tuple] = set()
    gates_without_sources: list[str] = []
    for req in rows:
        stored = req.sources or (req.context_snapshot or {}).get("sources") or []
        if not stored:
            gates_without_sources.append(req.type)
            continue
        for item in stored:
            key = (item.get("kind"), item.get("ref"), json.dumps(item.get("locator") or {}, sort_keys=True))
            if key in seen:      # 同一出处被多个 Gate 引用时只记一次，保留首次确认时间
                continue
            seen.add(key)
            entries.append({**item, "gate": req.type, "approval_id": req.id, "recorded_at": req.created_at.isoformat()})

    by_kind: dict[str, int] = {}
    for item in entries:
        by_kind[item.get("kind", "unknown")] = by_kind.get(item.get("kind", "unknown"), 0) + 1

    return {
        "task_ref": task_ref,
        "tenant": tenant,
        "gates": [r.type for r in rows],
        "sources": entries,
        "counts_by_kind": by_kind,
        "gates_without_sources": sorted(set(gates_without_sources)),
        "has_provenance": bool(entries),
    }


@router.post("/approvals/{approval_id}/decision", response_model=DecisionOut)
def decide(
    approval_id: str,
    payload: DecisionIn,
    db: Session = Depends(get_db),
    principal=Depends(require_role(*approver_roles())),
    tenant: str = Depends(current_tenant),
) -> Decision:
    """人做决策（Web 审批台入口）。

    鉴权（鉴权专项）：`AUTH_MODE=oidc` 时不仅要登录，还必须具备 approver/admin 角色——
    此前任何登录用户（含只读账号）都能批准审批。`AUTH_MODE=off/static` 行为不变。
    """
    req = _get_or_404(db, approval_id, tenant=tenant, for_update=True)
    _check_pending(req)
    _check_decision_allowed(req, payload.decision)    # 修复 BUG-013

    # 审计署名（鉴权专项）：OIDC 下身份是真实的，署名以 `sub` 为准——
    # 否则登录用户可以在审计里署别人的名字（"谁批的"就成了自述）。
    # static/off 模式没有真实身份（共享令牌/无认证），此时保留填写的姓名，并明确它是自述。
    if principal.sub not in ("anonymous", "static-token") and payload.decided_by \
            and payload.decided_by != principal.sub:
        logger.warning("决策署名与登录主体不一致：填写=%r 主体=%r（以主体为准）",
                       payload.decided_by, principal.sub)
    decided_by = principal.sub if principal.sub not in ("anonymous", "static-token") else (payload.decided_by or principal.sub)
    decision = Decision(
        request_id=req.id,
        version=req.version,
        decision=payload.decision,
        decided_by=decided_by,
        comment=payload.comment,
    )
    db.add(decision)

    if payload.decision == "approve":
        req.status = "approved"
    elif payload.decision == "reject":
        req.status = "rejected"
        apply_penalty(db, req.type, f"否决：{payload.comment}", tenant=req.tenant)
    else:  # revise：退回修订，允许修订后重新提交（version+1）
        req.status = "revised"
        apply_penalty(db, req.type, f"退回：{payload.comment}", tenant=req.tenant)

    try:
        db.commit()
    except IntegrityError as exc:  # 并发决策的第二道防线（唯一键 uq_decision_request_version）
        db.rollback()
        logger.warning("并发决策被唯一键拦下: approval=%s version=%s", approval_id, req.version)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该审批已被并发处理，请刷新后重试"
        ) from exc
    db.refresh(decision)
    # 发布 approval.decided 事件（契约 contracts/events.md），驱动工作流恢复/回退
    _publisher.publish(
        new_event(
            "approval.decided",
            {
                "approval_id": req.id,
                "task_ref": req.task_ref,
                "decision": decision.decision,
                "decided_by": decision.decided_by,
                "comment": decision.comment,
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    logger.info(
        "approval.decided id=%s decision=%s by=%s comment=%s",
        req.id, decision.decision, decision.decided_by, decision.comment,
    )
    return decision


@router.post("/approvals/{approval_id}/resubmit", response_model=ApprovalRequestOut)
def resubmit(
    approval_id: str,
    payload: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    principal=Depends(current_principal),
    tenant: str = Depends(current_tenant),
) -> ApprovalRequest:
    """revise 后修订重新提交：新上下文（version+1），重新进入 pending。

    **租户过滤（修复 BUG-002）**：此前 `_get_or_404` 未传 tenant，其它租户可把
    他人单据改回 pending（跨租户写）。现在与兄弟路由一致做隔离。
    """
    req = _get_or_404(db, approval_id, tenant=tenant, for_update=True)
    if req.status != "revised":
        raise HTTPException(status.HTTP_409_CONFLICT, "仅 revised 状态的审批可重新提交")

    req.context_snapshot = payload.context_snapshot.model_dump()
    req.deadline = payload.deadline
    req.escalation_policy = payload.escalation_policy.model_dump()
    req.status = "pending"
    req.version += 1
    db.commit()
    db.refresh(req)
    get_notifier().send(req)
    _publisher.publish(
        new_event(
            "approval.requested",
            {
                "approval_id": req.id,
                "gate": req.type,
                "stage": req.stage,
                "task_ref": req.task_ref,
                "requester": req.requester,
                "deadline": req.deadline.isoformat(),
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    return req


@router.post("/callbacks/im/card", response_model=DecisionOut)
async def im_card_callback(
    payload: ImCardCallback,
    request: Request,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> Decision:
    """IM 交互卡片回调：卡片按钮 → 决策。

    **鉴权（修复 BUG-001）**：飞书签名校验（`app/im_auth.py`）+ 租户隔离。
    未配置签名密钥时**拒绝**（503），不再允许任何匿名回调直接批准审批。
    """
    raw_body = await request.body()
    try:
        im_auth.verify(dict(request.headers), raw_body)
    except im_auth.ImAuthError as exc:
        logger.warning("IM 回调被拒：%s（%s）", exc, exc.reason)
        raise HTTPException(exc.status_code, f"IM 回调鉴权失败：{exc}") from exc

    req = _get_or_404(db, payload.approval_id, tenant=tenant, for_update=True)
    _check_pending(req)
    _check_decision_allowed(req, payload.action)      # 修复 BUG-013

    decision = Decision(
        request_id=req.id,
        version=req.version,
        decision=payload.action,
        decided_by=payload.operator,
        comment=payload.comment,
    )
    db.add(decision)
    if payload.action == "approve":
        req.status = "approved"
    elif payload.action == "reject":
        req.status = "rejected"
        apply_penalty(db, req.type, f"否决(IM)：{payload.comment}", tenant=req.tenant)
    else:
        req.status = "revised"
        apply_penalty(db, req.type, f"退回(IM)：{payload.comment}", tenant=req.tenant)
    try:
        db.commit()
    except IntegrityError as exc:  # 并发决策（IM 与 Web 同时点）——唯一键兜底
        db.rollback()
        logger.warning("并发决策被唯一键拦下(IM): approval=%s version=%s", payload.approval_id, req.version)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该审批已被并发处理，请刷新后重试"
        ) from exc
    db.refresh(decision)
    _publisher.publish(
        new_event(
            "approval.decided",
            {
                "approval_id": req.id,
                "task_ref": req.task_ref,
                "decision": decision.decision,
                "decided_by": decision.decided_by,
                "comment": decision.comment,
                "channel": "im",
                "version": req.version,
            },
            "approval-gateway",
        )
    )
    logger.info(
        "approval.decided(im) id=%s decision=%s by=%s", req.id, decision.decision, decision.decided_by
    )
    return decision


@router.get("/auth/info")
def auth_info(principal=Depends(current_principal)) -> dict:
    """当前认证配置与主体（前端可据此决定是否跳转 SSO 登录）。"""
    return {**auth_summary(), "principal": {"sub": principal.sub, "roles": principal.roles}}


@router.get("/trust")
def trust_all(db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> list[dict]:
    """本产品线（X-Tenant）各 Gate 的信任等级与指标（Phase 3 动态升降级）。

    注意：信任按 **产品线 × Gate** 隔离——新产品线从零积累，不继承其它产品线的 L1。
    """
    return all_trust(db, tenant=tenant)


@router.get("/trust/overview")
def trust_overview(db: Session = Depends(get_db)) -> list[dict]:
    """平台管理员总览：各产品线的 Gate 数、等级分布、自动放行占比（多产品线推广看板）。"""
    return tenant_summary(db)


@router.get("/trust/{gate_type}")
def trust_one(gate_type: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> dict:
    return compute_trust(db, gate_type, tenant=tenant)


@router.post("/admin/sweep")
def admin_sweep(db: Session = Depends(get_db),
                principal=Depends(require_role(*approver_roles()))) -> dict:
    """手动触发超时扫描（等价于 python -m app.sweep）。

    鉴权（鉴权专项）：这是**跨全部租户**的管理动作（会把所有到期未决审批按策略自动处置），
    此前只要求"登录"。现在要求 approver/admin 角色。
    """
    from .sweeper import sweep

    handled = sweep(db)
    return {"handled": len(handled), "items": handled}


# --------------------------------------------------------------------------------------
# 工程闸门（WP2，E1–E3）：机器判定"实施条件是否具备"，与人审 G 闸门互补
# --------------------------------------------------------------------------------------
@router.get("/eng-gates")
def eng_gates_list() -> list[dict]:
    """列出已定义的工程闸门规则（含规则指纹，便于确认判定依据的版本）。"""
    return eng_gates.list_rules()


@router.post("/eng-gates/{code}/check", response_model=GateCheckOut)
def eng_gate_check(
    code: str,
    payload: GateCheckIn,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
    principal=Depends(current_principal),
) -> dict:
    """执行工程闸门检查。

    **调用方契约**：返回 `blocked=true` 时必须停止推进（不得"只提示继续做"）。
    上下文可只给关键字段，来源链/依据会按 task_ref 从审批记录自动补齐。
    """
    code = code.upper()
    if code not in eng_gates.KNOWN_CODES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未知工程闸门 {code}（可用：{', '.join(eng_gates.KNOWN_CODES)}）")
    # 修复 BUG-003/014：显式忽略客户端传入的 mode/persist——宽严以服务端规则与
    # ENGINEERING_GATES_MODE 为准，留痕一律强制；客户端试图覆盖时记录告警。
    if payload.mode is not None or payload.persist is not None:
        logger.warning(
            "忽略客户端试图覆盖闸门控制项 task=%s mode=%s persist=%s（服务端权威）",
            payload.task_ref or "-", payload.mode, payload.persist,
        )
    try:
        return eng_gates.run_check(
            db,
            code=code,
            task_ref=payload.task_ref,
            tenant=tenant,
            context=payload.context,
            mode=None,          # 服务端权威：不接受调用方覆盖
            checked_by=payload.checked_by or principal.sub,
            persist=True,       # 审计强制
        )
    except gate_rules.GateRuleError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"规则加载失败：{exc}") from exc


@router.get("/eng-gates/checks")
def eng_gate_history(
    task_ref: str = "",
    code: str = "",
    limit: int = 50,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
) -> list[dict]:
    """检查历史（计划 §8 度量：拦截率、缺口补齐时长、误拦反馈的原始数据）。"""
    return eng_gates.history(db, tenant=tenant, task_ref=task_ref, code=code.upper(), limit=limit)


# --------------------------------------------------------------------------------------
# 证据库（WP5）：验收项 ↔ 可复验工件（课程图 33）
# --------------------------------------------------------------------------------------
@router.post("/evidence", status_code=status.HTTP_201_CREATED)
async def upload_evidence(
    request: Request,
    task_ref: str = Form(...),
    acceptance_item: str = Form(...),
    kind: str = Form("other"),
    note: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
    principal=Depends(current_principal),
) -> dict:
    """上传一件证据（multipart）。必须指明对应的验收项，否则无法用于交付判定。"""
    content = await file.read()
    try:
        return evidence.store(
            db,
            tenant=tenant,
            task_ref=task_ref,
            acceptance_item=acceptance_item,
            kind=kind,
            filename=file.filename or "evidence",
            content=content,
            created_by=principal.sub,
            note=note,
        )
    except evidence.EvidenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/evidence")
def list_evidence(task_ref: str = "", db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> dict:
    """按任务列出证据 + 验收项覆盖情况（E3 直接消费）。"""
    items = evidence.list_for_task(db, tenant, task_ref) if task_ref else []
    return {
        "task_ref": task_ref,
        "tenant": tenant,
        "evidence": items,
        "by_acceptance_item": evidence.evidence_map(db, tenant, task_ref) if task_ref else {},
    }


@router.get("/evidence/verify")
def verify_evidence(task_ref: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> dict:
    """复验：逐件校验 sha256（课程图 33：证据要能复验，不能只写"已测"）。"""
    return evidence.verify(db, tenant, task_ref)


@router.get("/evidence/{artifact_id}")
def evidence_meta(artifact_id: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> dict:
    row = evidence.get(db, tenant, artifact_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"证据 {artifact_id} 不存在")
    return evidence.to_dict(row)


@router.get("/evidence/{artifact_id}/content")
def evidence_content(artifact_id: str, db: Session = Depends(get_db), tenant: str = Depends(current_tenant)):
    row = evidence.get(db, tenant, artifact_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"证据 {artifact_id} 不存在")
    try:
        data = evidence.read_content(row)
    except evidence.EvidenceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{row.filename}"',
                             "X-Evidence-Sha256": row.sha256})


# --------------------------------------------------------------------------------------
# 误拦反馈（WP10 / M1 决策 D3）：让"误拦率"成为可计算指标
# --------------------------------------------------------------------------------------
@router.post("/eng-gates/checks/{check_id}/feedback")
def gate_check_feedback(
    check_id: str,
    payload: GateFeedbackIn,
    db: Session = Depends(get_db),
    tenant: str = Depends(current_tenant),
    principal=Depends(current_principal),
) -> dict:
    """人对某次拦截给出判定：确实该拦（valid）／其实该放行（false_positive）。

    计划 §8 约定误拦率 <10%；反馈覆盖率不足时该指标不可信（`feedback_stats` 会如实报出）。
    """
    try:
        return eng_gates.record_feedback(
            db, tenant=tenant, check_id=check_id, verdict=payload.verdict,
            note=payload.note, by=principal.sub or payload.by,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/eng-gates/feedback/stats")
def gate_feedback_stats(db: Session = Depends(get_db), tenant: str = Depends(current_tenant)) -> dict:
    """误拦率统计（计划 §8 度量；`false_positive_rate` 为 None 表示尚无足够反馈）。"""
    return eng_gates.feedback_stats(db, tenant=tenant)
