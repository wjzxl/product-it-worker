"""任务中心入口。"""
import logging

from fastapi import FastAPI

from .db import Base, engine
from .router import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

Base.metadata.create_all(bind=engine)


def _ensure_tenant_column() -> None:
    """轻量迁移：为既有库补 tenant 列（多租户）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("tasks")]
        if "tenant" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN tenant VARCHAR(64) DEFAULT 'default'"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_tenant ON tasks (tenant)"))
            logging.getLogger(__name__).info("已为 tasks 补充 tenant 列")
    except Exception:
        logging.getLogger(__name__).exception("tenant 列迁移失败")


_ensure_tenant_column()

app = FastAPI(
    title="task-center",
    description="平台任务中心：统一任务状态机 + 时间线 + 平台事件接入（设计文档 02 §0）",
    version="0.1.0",
)

app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "task-center", "version": "0.1.0"}
