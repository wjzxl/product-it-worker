"""Qdrant 存储封装（Phase 0：单 collection）。

多产品线/多模型注意：collection 的向量维度由**建库时的 embedder** 决定。
换 embedder（例如 HashEmbedder 8 维 → qwen-embed 1024 维）必须换 collection 名，
否则写入必然 400。此处显式校验维度并给出可执行的报错，避免只看到 Qdrant 的
"Vector dimension error" 原始信息。
"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams


class VectorDimMismatch(RuntimeError):
    """collection 维度与当前 embedder 不一致（换模型未换 collection）。"""


class QdrantStore:
    """collection **按实际向量维度惰性创建**：不在启动时猜维度，首次写入时以真实向量为准。

    这样换 embedder（含私有化模型）只需换 collection 名，无需预先知道维度。
    """

    def __init__(self, url: str, collection: str, dim: int):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.dim = dim  # 期望维度（未校准时为占位值）
        self.calibrated = False

    def _collection_dim(self) -> int | None:
        try:
            if not self.client.collection_exists(self.collection):
                return None
            info = self.client.get_collection(self.collection)
        except Exception:
            return None
        vectors = getattr(info.config.params, "vectors", None)
        return getattr(vectors, "size", None)

    def ensure(self, dim: int | None = None) -> int:
        """创建或校验 collection 维度。`dim` 为实际向量维度（None 时用期望值）。"""
        want = dim or self.dim
        existing = self._collection_dim()
        if existing is None:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=want, distance=Distance.COSINE),
            )
            self.dim, self.calibrated = want, True
            return want
        if existing != want:
            raise VectorDimMismatch(self._mismatch_msg(existing, want))
        self.dim, self.calibrated = want, True
        return want

    # 兼容旧调用名
    def _ensure_collection(self) -> None:
        self.ensure()

    def _mismatch_msg(self, existing: int, want: int | None = None) -> str:
        want = want or self.dim
        return (
            f"collection `{self.collection}` 向量维度为 {existing}，当前 embedder 输出 {want} 维。"
            f"更换 embedder 时请同时更换 collection（如 KB_COLLECTION={self.collection}_{want}），"
            f"或删除旧 collection 后重建（旧向量与当前模型不兼容，混用会导致检索结果无意义）。"
        )

    def describe(self) -> dict:
        """自检信息：供 /health 与产品线接入脚本判断知识库是否可用。"""
        actual = self._collection_dim()
        out = {
            "collection": self.collection,
            "expected_dim": self.dim if self.calibrated else None,
            "actual_dim": actual,
            "calibrated": self.calibrated,
            "ok": True,
        }
        if actual is None:
            out["note"] = "collection 尚未创建：首次写入时按实际向量维度自动创建"
        elif self.calibrated and actual != self.dim:
            out["ok"] = False
            out["error"] = self._mismatch_msg(actual)
        return out

    def upsert(self, chunks: list[dict], vectors: list[list[float]]) -> int:
        if vectors:
            self.ensure(len(vectors[0]))  # 以真实向量维度为准建库/校验
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
        self.ensure(len(vector))  # 首次检索前也按真实查询向量维度校准
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

    def describe(self) -> dict:
        return {"collection": "memory", "expected_dim": self.dim, "actual_dim": self.dim, "ok": True}

    def upsert(self, chunks: list[dict], vectors: list[list[float]]) -> int:
        if vectors and len(vectors[0]) != self.dim:
            raise VectorDimMismatch(
                f"写入向量为 {len(vectors[0])} 维，与存储期望的 {self.dim} 维不一致（embedder 与 collection 不匹配）。"
            )
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
