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


def test_g7_gate_is_labeled():
    """G7 流程改进闸门必须有中文标签与平台域归属（Phase 3 自进化提案闸门）。"""
    from app.metrics import DOMAIN_OF_GATE, GATE_LABEL

    assert GATE_LABEL["GATE_PROCESS_IMPROVEMENT"] == "G7 流程改进"
    assert DOMAIN_OF_GATE["GATE_PROCESS_IMPROVEMENT"] == "平台域"
    # 七个 Gate 全部有标签，避免新增 Gate 后漏配（回归：G7 曾缺失）
    from app.metrics import GATE_LABEL as labels

    assert len(labels) == 7
    assert all(v.startswith(f"G{i} ") for i, v in enumerate(labels.values(), start=1))


def test_collect_labels_g7_when_present():
    """真实库里若有 G7 审批，面板必须显示中文标签而不是原始键名。"""
    data = collect()
    for g in data["gates"]:
        assert not g["gate"].startswith("GATE_"), f"未映射标签：{g['gate']}"
