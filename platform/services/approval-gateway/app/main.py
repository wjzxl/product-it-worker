"""审批网关入口：审批 API + 超时扫描（可选定时循环）+ Web 审批台静态页。"""
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import Base, SessionLocal, engine
from .router import router
from .sweeper import sweep
from .service_auth import install_service_auth, security_posture

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# 建表（Phase 0 起步用；生产切 PG 后引入 Alembic 迁移）
Base.metadata.create_all(bind=engine)


def _ensure_tenant_column() -> None:
    """轻量迁移：为既有库补 tenant 列（多租户）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("approval_requests")]
        if "tenant" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE approval_requests ADD COLUMN tenant VARCHAR(64) DEFAULT 'default'"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_approval_requests_tenant ON approval_requests (tenant)"))
            logger.info("已为 approval_requests 补充 tenant 列（多租户迁移）")
    except Exception:  # 迁移失败不阻断服务（表可能尚不存在）
        logger.exception("tenant 列迁移失败")


def _ensure_sources_column() -> None:
    """WP1 轻量迁移：为既有库补 sources 列（需求来源链）。

    只加列不删列；存量行 sources 为 NULL，读取时按空列表处理（不视为缺失）。
    """
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("approval_requests")]
        if "sources" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE approval_requests ADD COLUMN sources JSON"))
            logger.info("已为 approval_requests 补充 sources 列（WP1 需求来源链迁移）")
    except Exception:  # 迁移失败不阻断服务（表可能尚不存在）
        logger.exception("sources 列迁移失败")


def _ensure_gate_check_table() -> None:
    """WP2 轻量迁移：工程闸门检查留痕表（E1–E3）。SQLAlchemy 建表，缺表即建。"""
    from sqlalchemy import inspect

    try:
        insp = inspect(engine)
        if "gate_checks" not in insp.get_table_names():
            Base.metadata.tables["gate_checks"].create(bind=engine)
            logger.info("已创建 gate_checks 表（WP2 工程闸门留痕）")
    except Exception:
        logger.exception("gate_checks 建表失败")


def _ensure_evidence_table() -> None:
    """WP5 轻量迁移：证据工件表（缺表即建）。"""
    from sqlalchemy import inspect

    try:
        insp = inspect(engine)
        if "evidence_artifacts" not in insp.get_table_names():
            Base.metadata.tables["evidence_artifacts"].create(bind=engine)
            logger.info("已创建 evidence_artifacts 表（WP5 证据库）")
    except Exception:
        logger.exception("evidence_artifacts 建表失败")


def _ensure_gate_feedback_columns() -> None:
    """WP10 轻量迁移：为既有 gate_checks 补误拦反馈列（只加列不删列）。

    误拦反馈是"误拦率 <10%"唯一的数据来源（M1 决策 D3）——没有它闸门只能靠感觉调宽严。
    """
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "gate_checks" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("gate_checks")]
        additions = {
            "feedback": "VARCHAR(32) DEFAULT ''",
            "feedback_note": "TEXT DEFAULT ''",
            "feedback_by": "VARCHAR(128) DEFAULT ''",
            "feedback_at": "TIMESTAMP",
        }
        missing = {name: ddl for name, ddl in additions.items() if name not in cols}
        if missing:
            with engine.begin() as conn:
                for name, ddl in missing.items():
                    conn.execute(text(f"ALTER TABLE gate_checks ADD COLUMN {name} {ddl}"))
            logger.info("已为 gate_checks 补充反馈列：%s", ", ".join(missing))
    except Exception:
        logger.exception("gate_checks 反馈列迁移失败")


def _ensure_decision_version_column() -> None:
    """并发专项修复的轻量迁移：`approval_decisions.version` 列 + (request_id, version) 唯一键。

    没有它，并发决策会往同一单据写多条决策（实测 30 并发 8 轮中 6 轮 2–5 条）。
    存量库可能已经存在重复行（正是本缺陷造成的），因此先把重复行的 version 顺次编号
    （保留全部审计记录、不改写谁批的），再建唯一索引；任何失败只记日志、不阻断启动。
    """
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "approval_decisions" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("approval_decisions")]
        with engine.begin() as conn:
            if "version" not in cols:
                conn.execute(text("ALTER TABLE approval_decisions ADD COLUMN version INTEGER DEFAULT 1"))
                logger.info("已为 approval_decisions 补充 version 列（并发防重复决策迁移）")
            # 存量重复行顺次编号，避免建唯一索引失败（历史审计数据一条不删）
            if engine.dialect.name == "postgresql":
                conn.execute(text(
                    "UPDATE approval_decisions d SET version = s.rn FROM ("
                    "  SELECT id, row_number() OVER (PARTITION BY request_id ORDER BY decided_at, id) AS rn"
                    "  FROM approval_decisions"
                    ") s WHERE d.id = s.id AND d.version IS DISTINCT FROM s.rn"
                ))
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_request_version "
                "ON approval_decisions (request_id, version)"
            ))
        logger.info("approval_decisions 唯一键 uq_decision_request_version 就绪")
    except Exception:  # 迁移失败不阻断服务（表可能尚不存在 / 非 PG 方言）
        logger.exception("approval_decisions version/唯一键迁移失败")


_ensure_tenant_column()
_ensure_sources_column()
_ensure_gate_check_table()
_ensure_gate_feedback_columns()
_ensure_evidence_table()
_ensure_decision_version_column()

app = FastAPI(
    title="平台人审网关 approval-gateway",
    description="统一审批状态机 + Web/IM 双通道 + 超时升级（设计文档 03 §3）",
    version="0.1.0",
)
# IM 卡片回调自带飞书签名校验；审批台是纯静态页，其资源按**目录内容**豁免
# （新增 js/css 时不需要记得回来改豁免清单——漏改的表现是页面白屏）
install_service_auth(app, "approval-gateway", extra_exempt=("/callbacks/im", "/"),
                     static_dir=Path(__file__).parent / "static")

app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "approval-gateway", "version": "0.1.0",
            "security": security_posture("approval-gateway")}


# Web 审批台（静态单页，挂载在最后，不影响 /docs 与 API 路由）
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="console")


# 可选：内置超时扫描循环（与 python -m app.sweep / POST /admin/sweep 三选一）
_sweep_interval = int(os.getenv("SWEEP_INTERVAL_SECONDS", "0") or 0)
if _sweep_interval > 0:

    def _sweep_loop() -> None:
        while True:
            time.sleep(_sweep_interval)
            try:
                with SessionLocal() as db:
                    handled = sweep(db)
                    if handled:
                        logger.info("定时扫描处置 %d 条超时审批", len(handled))
            except Exception:  # noqa: BLE001 —— 扫描失败不能杀死服务
                logger.exception("定时扫描异常")

    threading.Thread(target=_sweep_loop, daemon=True, name="sweeper").start()
    logger.info("超时扫描循环已启动（每 %ds）", _sweep_interval)
