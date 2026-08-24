"""知识库 v0 入口：文档入库 + 检索（带来源）。

Phase 0 说明：设计文档原选型 RAGFlow；实施时收敛为「Qdrant + LiteLLM embeddings」
的轻量链路（Qdrant 已在 compose 中、链路可控可测），RAGFlow 作为 Phase 1 的可选升级
（解析/分块更精细、内置编排）。两者对外接口一致，可无缝替换。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .embedder import embedder_from_env
from .ingest import chunk_markdown
from .store import MemoryStore, QdrantStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class DocumentIn(BaseModel):
    source: str = Field(min_length=1, description="来源标识（文档名/链接）")
    content: str = Field(min_length=1)


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    limit: int = 5


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
    app.state.store = store
    app.state.embedder = embedder

    @app.post("/documents")
    def ingest(document: DocumentIn) -> dict:
        chunks = chunk_markdown(document.content, document.source)
        if not chunks:
            raise HTTPException(status_code=422, detail="文档内容为空")
        vectors = embedder.embed([c["text"] for c in chunks])
        n = store.upsert(chunks, vectors)
        return {"source": document.source, "chunks": n}

    @app.post("/search")
    def search(body: SearchIn) -> list[ChunkOut]:
        vector = embedder.embed([body.query])[0]
        hits = store.search(vector, limit=body.limit)
        return [ChunkOut(**h) for h in hits]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "knowledge-base", "version": "0.1.0"}

    return app


app = create_app()
