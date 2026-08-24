"""向量化：经 LiteLLM 网关调 embeddings；无 Key 时用确定性哈希桩（离线可跑）。"""
from __future__ import annotations

import hashlib
import os

import httpx


class LiteLLMEmbedder:
    """OpenAI 兼容 /embeddings（目标：LiteLLM 网关）。维度由 EMBEDDING_DIM 指定。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = int(os.getenv("EMBEDDING_DIM", "1536"))
        self._client = httpx.Client(timeout=60, transport=transport)

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


class HashEmbedder:
    """确定性哈希桩：同文本恒同向量（8 维），用于离线演示与测试。"""

    dim = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            digest = hashlib.md5(text.encode("utf-8")).digest()
            vectors.append([(b / 255.0) * 2 - 1 for b in digest[: self.dim]])
        return vectors


def embedder_from_env():
    key = os.getenv("LLM_API_KEY", "")
    if key:
        return LiteLLMEmbedder(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:4000/v1"),
            api_key=key,
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        )
    return HashEmbedder()
