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


@pytest.fixture()
def env():
    store = MemoryStore(dim=HashEmbedder.dim)
    app = create_app(store=store, embedder=HashEmbedder())
    return {"client": TestClient(app), "store": store}


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
