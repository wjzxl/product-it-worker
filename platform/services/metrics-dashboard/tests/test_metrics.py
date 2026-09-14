"""度量面板测试：指标采集结构 + 面板渲染（PG 不可达时跳过）。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.metrics import collect


def _pg_available() -> bool:
    try:
        data = collect()
        return "approvals_total" in data
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="需要 PostgreSQL（平台库）")


def test_metrics_structure():
    data = collect()
    assert isinstance(data["approvals_total"], int)
    assert isinstance(data["gates"], list)
    for g in data["gates"]:
        assert {"gate", "domain", "count"} <= set(g)


def test_dashboard_html():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "平台度量面板" in r.text and "Gate 决策" in r.text and "LLM 成本" in r.text


def test_health():
    assert TestClient(app).get("/health").json()["service"] == "metrics-dashboard"
