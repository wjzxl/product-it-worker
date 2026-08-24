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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# 建表（Phase 0 起步用；生产切 PG 后引入 Alembic 迁移）
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="平台人审网关 approval-gateway",
    description="统一审批状态机 + Web/IM 双通道 + 超时升级（设计文档 03 §3）",
    version="0.1.0",
)

app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "approval-gateway", "version": "0.1.0"}


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
