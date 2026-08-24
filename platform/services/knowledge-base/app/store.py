"""Qdrant 存储封装（Phase 0：单 collection）。"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams


class QdrantStore:
    def __init__(self, url: str, collection: str, dim: int):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.dim = dim
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def upsert(self, chunks: list[dict], vectors: list[list[float]]) -> int:
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={"text": chunk["text"], "source": chunk["source"], "index": chunk["index"]},
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        if points:
            self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(self, vector: list[float], limit: int = 5) -> list[dict]:
        hits = self.client.search(
            collection_name=self.collection,
            query_vector=vector,
            limit=limit,
        )
        return [
            {
                "text": hit.payload.get("text", ""),
                "source": hit.payload.get("source", ""),
                "score": hit.score,
            }
            for hit in hits
        ]


class MemoryStore:
    """测试/离线桩：内存存储 + 余弦相似度检索。"""

    def __init__(self, dim: int):
        self.dim = dim
        self.points: list[dict] = []

    def upsert(self, chunks: list[dict], vectors: list[list[float]]) -> int:
        for chunk, vector in zip(chunks, vectors):
            self.points.append({"payload": chunk, "vector": vector})
        return len(chunks)

    def search(self, vector: list[float], limit: int = 5) -> list[dict]:
        def cosine(a: list[float], b: list[float]) -> float:
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(x * x for x in b) ** 0.5
            if not norm_a or not norm_b:
                return 0.0
            return sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)

        scored = sorted(
            ((cosine(vector, p["vector"]), p) for p in self.points),
            key=lambda t: t[0],
            reverse=True,
        )
        return [
            {"text": p["payload"]["text"], "source": p["payload"]["source"], "score": score}
            for score, p in scored[:limit]
        ]
