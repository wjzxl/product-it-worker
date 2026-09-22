"""鉴权专项回归（knowledge-base）：服务间令牌中间件。

清点结论（`platform/scripts/audit_endpoint_auth.py`）：`POST /documents`（入库）与
`POST /search` 原本没有任何鉴权依赖，任何能连上端口的人都可投毒知识库 / 拉取全部知识。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.embedder import HashEmbedder
from app.main import create_app
from app.store import MemoryStore


@pytest.fixture()
def client():
    app = create_app(store=MemoryStore(dim=HashEmbedder.dim), embedder=HashEmbedder())
    with TestClient(app) as c:
        yield c


class TestServiceToken:
    def test_ingest_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/documents", json={"doc_id": "d1", "text": "# 标题\n正文"})
        assert r.status_code == 401, r.text

    def test_search_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.post("/search", json={"query": "导出"}).status_code == 401

    def test_correct_token_passes(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/documents", json={"doc_id": "d2", "text": "# 标题\n正文"},
                        headers={"X-Service-Token": "svc"})
        assert r.status_code != 401, r.text

    def test_health_exempt_and_prefix_not_overbroad(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.get("/health").status_code == 200
        assert client.post("/health-admin").status_code == 401

    def test_token_mode_without_secret_fails_closed(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.delenv("SERVICE_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AUTH_STATIC_TOKEN", raising=False)
        r = client.post("/search", json={"query": "导出"})
        assert r.status_code == 503, "token 模式但没配密钥时必须拒绝，而不是放行"

    def test_reads_allowed_when_disabled(self, client):
        assert client.get("/health").status_code == 200
