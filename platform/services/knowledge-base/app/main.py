"""知识库 v0 入口：文档入库 + 检索（带来源）。

Phase 0 说明：设计文档原选型 RAGFlow；实施时收敛为「Qdrant + LiteLLM embeddings」
的轻量链路（Qdrant 已在 compose 中、链路可控可测），RAGFlow 作为 Phase 1 的可选升级
（解析/分块更精细、内置编排）。两者对外接口一致，可无缝替换。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .embedder import EmbeddingDimError, embedder_from_env
from .ingest import chunk_markdown
from .store import MemoryStore, QdrantStore, VectorDimMismatch
from .service_auth import install_service_auth, security_posture

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class DocumentIn(BaseModel):
    source: str = Field(min_length=1, description="来源标识（文档名/链接）")
    content: str = Field(min_length=1)


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    limit: int = 5
    source_prefix: str = Field(
        default="",
        description="只返回该来源前缀下的知识（多产品线隔离：如 `line-pay/`；空=不限，检索全平台）",
    )


class ChunkOut(BaseModel):
    text: str
    source: str
    score: Optional[float] = None


def create_app(store=None, embedder=None) -> FastAPI:
    embedder = embedder or embedder_from_env()
    if store is None:
        qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
        collection = os.getenv("KB_COLLECTION", "platform_kb")
        dim = getattr(embedder, "dim", 1536)
        try:
            store = QdrantStore(qdrant_url, collection, dim=dim)
        except Exception as exc:  # Qdrant 不可达 → 降级内存桩，保证离线可跑
            logging.getLogger(__name__).warning("Qdrant 不可达，降级内存桩: %s", exc)
            store = MemoryStore(dim=dim)

    app = FastAPI(
        title="knowledge-base",
        description="知识库 v0：文档分块 → 向量化 → Qdrant 入库 → 带来源检索（设计文档 01 §4 能力层）",
        version="0.1.0",
    )
    install_service_auth(app, "knowledge-base")
    app.state.store = store
    app.state.embedder = embedder

    @app.post("/documents")
    def ingest(document: DocumentIn) -> dict:
        chunks = chunk_markdown(document.content, document.source)
        if not chunks:
            raise HTTPException(status_code=422, detail="文档内容为空")
        try:
            vectors = embedder.embed([c["text"] for c in chunks])
            n = store.upsert(chunks, vectors)
        except (VectorDimMismatch, EmbeddingDimError) as exc:
            # 配置错误（换 embedder 未换 collection / 维度声明不符）：明确报错，不落成 500
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=503, detail=f"向量化失败（embedder 网关）：{exc}") from exc
        return {"source": document.source, "chunks": n, "dim": getattr(store, "dim", None)}

    @app.post("/search")
    def search(body: SearchIn) -> list[ChunkOut]:
        try:
            vector = embedder.embed([body.query])[0]
            prefix = (body.source_prefix or "").strip()
            hits = store.search(vector, limit=body.limit * 3 if prefix else body.limit)
        except (VectorDimMismatch, EmbeddingDimError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=503, detail=f"向量化失败（embedder 网关）：{exc}") from exc
        prefix = (body.source_prefix or "").strip()
        if prefix:
            # 产品线隔离：先多取候选再按来源前缀过滤，避免过滤后结果过少
            hits = [h for h in hits if str(h.get("source", "")).startswith(prefix)]
        return [ChunkOut(**h) for h in hits[: body.limit]]

    @app.get("/health")
    def health() -> dict:
        store_info = store.describe()
        status = "ok" if store_info.get("ok", True) else "degraded"
        return {
            "status": status,
            "service": "knowledge-base",
            "version": "0.1.0",
            "security": security_posture("knowledge-base"),
            "embedder": type(embedder).__name__,
            "store": store_info,
        }

    return app


app = create_app()
