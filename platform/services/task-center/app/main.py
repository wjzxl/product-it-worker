"""任务中心入口。"""
import logging

from fastapi import FastAPI

from .db import Base, engine
from .router import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="task-center",
    description="平台任务中心：统一任务状态机 + 时间线 + 平台事件接入（设计文档 02 §0）",
    version="0.1.0",
)

app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "task-center", "version": "0.1.0"}
