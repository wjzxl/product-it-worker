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

def _ensure_task_ref_constraint() -> None:
    """轻量迁移：把 external_ref 的**全局唯一**改为 **(tenant, external_ref)** 复合唯一。

    背景：多租户下不同产品线可以引用各自系统的同名单号；全局唯一会让第二个租户建单 409。
    只动索引，不动数据。
    """
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "tasks" not in insp.get_table_names():
            return
        indexes = {i["name"]: i for i in insp.get_indexes("tasks")}
        with engine.begin() as conn:
            for name, meta in indexes.items():
                cols = list(meta.get("column_names") or [])
                if meta.get("unique") and cols == ["external_ref"]:
                    try:
                        conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
                        logger.info("已移除 external_ref 全局唯一索引 %s", name)
                    except Exception:  # PG 上可能是约束
                        try:
                            conn.execute(text(f'ALTER TABLE tasks DROP CONSTRAINT IF EXISTS "{name}"'))
                            logger.info("已移除 external_ref 全局唯一约束 %s", name)
                        except Exception:
                            logger.exception("移除 %s 失败（不影响启动）", name)
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_tenant_external_ref "
                "ON tasks (tenant, external_ref)"
            ))
    except Exception:
        logger.exception("tasks 唯一约束迁移失败")


_ensure_task_ref_constraint()
