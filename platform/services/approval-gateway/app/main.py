"""审批网关入口。"""
import logging

from fastapi import FastAPI

from .db import Base, engine
from .router import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

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
