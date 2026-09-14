"""知识库测试：分块、入库、带来源检索（内存桩 + 哈希向量，离线可测）。"""
import pytest
from fastapi.testclient import TestClient

from app.embedder import HashEmbedder
from app.ingest import chunk_markdown
from app.main import create_app
from app.store import MemoryStore

DOC_A = (
    "## 报表导出需求\n用户想要导出报表，支持按日期筛选。\n\n## 验收标准\n"
    "WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据。\nWHEN 用户点击导出 THEN 生成 CSV 文件。"
)
DOC_B = "## 权限体系\n导出功能仅对管理员开放，普通用户无导出权限。"


class _FakeClient:
    """仅用于维度校验路径：不连真实 Qdrant。"""

    def __init__(self, exists: bool = True):
        self._exists = exists

    def collection_exists(self, name: str) -> bool:
        return self._exists


@pytest.fixture()
def env():
    store = MemoryStore(dim=HashEmbedder.dim)
    app = create_app(store=store, embedder=HashEmbedder())
    return {"client": TestClient(app), "store": store, "app": app}


class TestChunking:
    def test_markdown_chunks(self):
        chunks = chunk_markdown(DOC_A, "prd-a.md")
        assert len(chunks) >= 1
        assert all(c["source"] == "prd-a.md" for c in chunks)
        assert chunks[0]["index"] == 0

    def test_long_text_windows(self):
        content = "x" * 1400
        chunks = chunk_markdown(content, "long.md")
        assert len(chunks) >= 3
        assert all(len(c["text"]) <= 500 for c in chunks)


class TestIngestAndSearch:
    def test_ingest_then_search_with_source(self, env):
        r = env["client"].post("/documents", json={"source": "prd-a.md", "content": DOC_A})
        assert r.status_code == 200
        assert r.json()["chunks"] >= 1
        env["client"].post("/documents", json={"source": "perm.md", "content": DOC_B})

        # 哈希向量：相同文本的余弦相似度最高 → 检索到含"日期筛选"的块
        r = env["client"].post("/search", json={"query": "按日期筛选导出报表", "limit": 3})
        assert r.status_code == 200
        hits = r.json()
        assert hits, "应命中至少一条"
        assert any("prd-a.md" in h["source"] for h in hits)
        assert all("text" in h and "source" in h and "score" in h for h in hits)

    def test_empty_document_rejected(self, env):
        r = env["client"].post("/documents", json={"source": "x.md", "content": "   "})
        assert r.status_code == 422


class TestVectorDimGuard:
    """换 embedder 未换 collection 的症状（真实踩坑：8 维哈希 → 1024 维 qwen-embed）。"""

    def test_qdrant_store_detects_dim_mismatch(self):
        from app.store import QdrantStore, VectorDimMismatch

        store = QdrantStore.__new__(QdrantStore)   # 绕过 __init__，不连真实 Qdrant
        store.client = _FakeClient(exists=True)    # collection 已存在
        store.collection = "platform_kb"
        store.dim = 1024
        store.calibrated = False
        store._collection_dim = lambda: 8          # 但只有 8 维
        with pytest.raises(VectorDimMismatch) as exc:
            store.ensure(1024)
        msg = str(exc.value)
        assert "8" in msg and "1024" in msg and "KB_COLLECTION" in msg   # 报错必须可执行

    def test_describe_reports_mismatch(self):
        from app.store import QdrantStore

        store = QdrantStore.__new__(QdrantStore)
        store.collection = "platform_kb"
        store.dim = 1024
        store.calibrated = True
        store._collection_dim = lambda: 8
        info = store.describe()
        assert info["ok"] is False and info["actual_dim"] == 8 and info["expected_dim"] == 1024

    def test_health_exposes_store_dim(self, env):
        body = env["client"].get("/health").json()
        assert body["status"] == "ok"
        assert body["store"]["ok"] is True and body["store"]["expected_dim"] == body["store"]["actual_dim"]

    def test_ingest_returns_409_on_dim_mismatch(self, env):
        from app.store import VectorDimMismatch

        def boom(chunks, vectors):
            raise VectorDimMismatch("collection 维度不匹配，请更换 KB_COLLECTION")

        original = env["app"].state.store.upsert
        env["app"].state.store.upsert = boom
        try:
            r = env["client"].post("/documents", json={"source": "x.md", "content": DOC_A})
        finally:
            env["app"].state.store.upsert = original
        assert r.status_code == 409 and "KB_COLLECTION" in r.json()["detail"]

    def test_ingest_returns_503_when_embedder_down(self, env):
        import httpx

        def boom(texts):
            raise httpx.HTTPStatusError("401", request=httpx.Request("POST", "http://gw"), response=httpx.Response(401))

        original = env["app"].state.embedder.embed
        env["app"].state.embedder.embed = boom
        try:
            r = env["client"].post("/documents", json={"source": "x.md", "content": DOC_A})
        finally:
            env["app"].state.embedder.embed = original
        assert r.status_code == 503 and "embedder" in r.json()["detail"]


class TestEmbeddingDimDiscovery:
    """私有化/换模型接入：维度自动发现 + 声明不符报错（真实踩坑：1536 假定 vs 1024 实际）。"""

    @staticmethod
    def _embedder(transport, dim_env=None, monkeypatch=None):
        from app.embedder import LiteLLMEmbedder

        if monkeypatch is not None:
            if dim_env is None:
                monkeypatch.delenv("EMBEDDING_DIM", raising=False)
            else:
                monkeypatch.setenv("EMBEDDING_DIM", str(dim_env))
        return LiteLLMEmbedder(base_url="http://gw/v1", api_key="k", model="qwen-embed", transport=transport)

    @staticmethod
    def _fake_transport(dim: int):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * dim}]})

        return httpx.MockTransport(handler)

    def test_dim_auto_discovered_when_unset(self, monkeypatch):
        emb = self._embedder(self._fake_transport(1024), None, monkeypatch)
        assert emb.dim == 1536  # 占位值
        emb.embed(["x"])
        assert emb.dim == 1024  # 首次调用后按真实维度校正

    def test_declared_dim_mismatch_raises(self, monkeypatch):
        from app.embedder import EmbeddingDimError

        emb = self._embedder(self._fake_transport(1024), 1536, monkeypatch)
        with pytest.raises(EmbeddingDimError) as exc:
            emb.embed(["x"])
        assert "1536" in str(exc.value) and "1024" in str(exc.value)

    def test_declared_dim_match_ok(self, monkeypatch):
        emb = self._embedder(self._fake_transport(1024), 1024, monkeypatch)
        assert len(emb.embed(["x"])[0]) == 1024

    def test_upsert_rejects_vector_dim_mismatch(self):
        from app.store import MemoryStore, VectorDimMismatch

        store = MemoryStore(dim=1536)
        with pytest.raises(VectorDimMismatch) as exc:
            store.upsert([{"text": "t", "source": "s", "index": 0}], [[0.0] * 1024])
        assert "1024" in str(exc.value) and "1536" in str(exc.value)

    def test_lazy_create_uses_actual_vector_dim(self):
        """collection 不存在时，按真实向量维度建库（私有化模型无需预知维度）。"""
        from app.store import QdrantStore

        created = {}

        class FakeClient:
            def collection_exists(self, name):
                return False

            def create_collection(self, collection_name, vectors_config):
                created["name"] = collection_name
                created["size"] = vectors_config.size

        store = QdrantStore.__new__(QdrantStore)
        store.client = FakeClient()
        store.collection = "platform_kb_qwen"
        store.dim = 1536      # 占位（错误）期望值
        store.calibrated = False
        store.ensure(1024)    # 实际向量维度
        assert created == {"name": "platform_kb_qwen", "size": 1024}
        assert store.dim == 1024 and store.calibrated is True

    def test_describe_before_calibration(self):
        from app.store import QdrantStore

        store = QdrantStore.__new__(QdrantStore)
        store.collection = "platform_kb_qwen"
        store.dim = 1536
        store.calibrated = False
        store._collection_dim = lambda: None
        info = store.describe()
        assert info["ok"] is True and info["actual_dim"] is None and info["expected_dim"] is None
        assert "首次写入" in info["note"]


class TestSourcePrefixIsolation:
    """多产品线知识隔离：A 产品线的 Agent 不应检索到 B 产品线的 Runbook。"""

    def test_prefix_filters_other_lines(self, env):
        client = env["client"]
        client.post("/documents", json={"source": "line-pay/runbooks/db.md", "content": "## 支付库连接池耗尽\n重启连接池并扩容。"})
        client.post("/documents", json={"source": "line-crm/runbooks/db.md", "content": "## CRM 库连接池耗尽\n重启连接池并扩容。"})

        scoped = client.post("/search", json={"query": "连接池耗尽", "limit": 5, "source_prefix": "line-pay/"}).json()
        assert scoped, "本产品线应有命中"
        assert all(h["source"].startswith("line-pay/") for h in scoped)

        other = client.post("/search", json={"query": "连接池耗尽", "limit": 5, "source_prefix": "line-crm/"}).json()
        assert all(h["source"].startswith("line-crm/") for h in other)

        # 不带前缀 = 全平台检索（平台级改进分析用）
        everything = client.post("/search", json={"query": "连接池耗尽", "limit": 5}).json()
        sources = {h["source"] for h in everything}
        assert any(s.startswith("line-pay/") for s in sources) or any(s.startswith("line-crm/") for s in sources)

    def test_prefix_with_no_match_returns_empty(self, env):
        env["client"].post("/documents", json={"source": "line-pay/runbooks/a.md", "content": "## 内容\n文本。"})
        hits = env["client"].post("/search", json={"query": "内容", "limit": 5, "source_prefix": "line-x/"}).json()
        assert hits == []
