"""向量化：经 LiteLLM 网关调 embeddings；无 Key 时用确定性哈希桩（离线可跑）。

维度处理（私有化/多模型接入的关键）：`EMBEDDING_DIM` **未设置时按首次返回的向量自动发现**，
设置了则作为契约校验——不一致直接报错。真实踩坑：DashScope `text-embedding-v3` 返回 1024 维，
而代码默认假定 1536 维，导致 Qdrant 建库维度错误、写入全部 400。
"""
from __future__ import annotations

import hashlib
import os

import httpx


class EmbeddingDimError(ValueError):
    """声明的向量维度与模型实际返回不一致。"""


class LiteLLMEmbedder:
    """OpenAI 兼容 /embeddings（目标：LiteLLM 网关）。维度由 EMBEDDING_DIM 指定或自动发现。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        declared = os.getenv("EMBEDDING_DIM", "")
        self.declared_dim = int(declared) if declared else dim
        self.dim = self.declared_dim or 1536  # 未知时先占位，首次调用后按真实值校正
        self._client = httpx.Client(timeout=60, transport=transport)

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        vectors = [d["embedding"] for d in data]
        self._sync_dim(vectors)
        return vectors

    def _sync_dim(self, vectors: list[list[float]]) -> None:
        if not vectors:
            return
        actual = len(vectors[0])
        if self.declared_dim and actual != self.declared_dim:
            raise EmbeddingDimError(
                f"EMBEDDING_DIM={self.declared_dim} 与模型 `{self.model}` 实际返回的 {actual} 维不一致；"
                f"请修正 EMBEDDING_DIM（或删除该变量让其自动发现），并确保 Qdrant collection 维度一致。"
            )
        if not self.declared_dim and actual != self.dim:
            self.dim = actual  # 自动发现：私有化模型换模型时无需改代码


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
