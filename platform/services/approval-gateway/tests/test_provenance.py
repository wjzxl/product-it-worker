"""WP1 需求来源链（provenance）测试。

验收口径来自 17-FDE范式落地开发计划.md WP1：
1. `sources` 缺省兼容（存量审批、既有 payload 不报错）；
2. 落库与反查（产物/task_ref → 出处，含载体精确定位）；
3. 跨 Gate 去重、缺出处的 Gate 被显式列出（供 E1 消费）；
4. 租户隔离（跨产品线不可见）。
"""
from __future__ import annotations  # Python 3.9：注解延迟求值，允许 list | None

import os
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

# 独立临时库，避免污染开发库
_tmpdir = tempfile.mkdtemp(prefix="wp1-provenance-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.main import app  # noqa: E402
from app.models import utcnow  # noqa: E402
from app.schemas import ContextSnapshot, SourceRef  # noqa: E402

VOICE_SOURCE = {
    "kind": "voice",
    "ref": "feishu://chat/oc_demo/msg/om_voice_1",
    "quote": "我们对账是按月分仓看的，不是按天。",
    "locator": {"start_ms": 41200, "end_ms": 47000},
    "captured_at": "2026-09-12T20:31:00+08:00",
    "confirmed_by": "李经理",
}
EXCEL_SOURCE = {
    "kind": "excel",
    "ref": "feishu://drive/file/月度对账.xlsx",
    "quote": "表头 11 列（含成本列）",
    "locator": {"sheet": "9月", "cell": "A1:K1"},
    "confirmed_by": "财务-王",
}


def _payload(task_ref: str, sources: list | None = None, gate: str = "GATE_REQUIREMENT_REVIEW") -> dict:
    snapshot = {
        "artifact_ref": "prd-export-v1",
        "diff_summary": "导出需求澄清与 PRD 草案",
        "evidence": ["https://example.com/prd"],
        "risk": {"score": 20, "level": "L1", "factors": ["常规"]},
    }
    if sources is not None:
        snapshot["sources"] = sources
    return {
        "type": gate,
        "stage": "产品域 G1",
        "requester": "产品Agent@issue-1",
        "task_ref": task_ref,
        "context_snapshot": snapshot,
        "deadline": (utcnow() + timedelta(hours=2)).isoformat(),
        "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
    }


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestSchemaCompat:
    def test_sources_is_optional(self):
        snap = ContextSnapshot(
            artifact_ref="a", diff_summary="d", evidence=["e"],
            risk={"score": 1, "level": "L1", "factors": []},
        )
        assert snap.sources == []          # 不传 = 空列表，不报错（存量 payload 兼容）

    def test_source_ref_requires_kind_and_ref(self):
        with pytest.raises(Exception):
            SourceRef(kind="voice")        # 缺 ref
        with pytest.raises(Exception):
            SourceRef(ref="x")             # 缺 kind

    def test_locator_defaults_to_empty(self):
        assert SourceRef(kind="chat", ref="x").locator == {}


class TestPersistAndQuery:
    def test_create_without_sources_still_works(self, client):
        r = client.post("/approvals", json=_payload("wp1-legacy"))
        assert r.status_code == 201
        body = r.json()
        assert body["context_snapshot"]["sources"] == []

    def test_sources_persisted_and_returned(self, client):
        r = client.post("/approvals", json=_payload("wp1-export", [VOICE_SOURCE, EXCEL_SOURCE]))
        assert r.status_code == 201, r.text
        snap = r.json()["context_snapshot"]
        assert len(snap["sources"]) == 2
        kinds = {s["kind"] for s in snap["sources"]}
        assert kinds == {"voice", "excel"}

    def test_provenance_lookup_returns_chain(self, client):
        client.post("/approvals", json=_payload("wp1-lookup", [VOICE_SOURCE]))
        client.post("/approvals", json=_payload("wp1-lookup", [EXCEL_SOURCE], gate="GATE_TECH_DESIGN_REVIEW"))

        r = client.get("/requirements/wp1-lookup/provenance")
        assert r.status_code == 200
        data = r.json()
        assert data["has_provenance"] is True
        assert data["counts_by_kind"] == {"voice": 1, "excel": 1}
        voice = next(s for s in data["sources"] if s["kind"] == "voice")
        # 出处必须带载体内的精确定位（课程图 11：消息ID/录音时间戳/单元格）
        assert voice["locator"]["start_ms"] == 41200
        assert "按月分仓" in voice["quote"]
        assert voice["confirmed_by"] == "李经理"
        assert voice["gate"] == "GATE_REQUIREMENT_REVIEW"

    def test_duplicate_source_deduped_across_gates(self, client):
        client.post("/approvals", json=_payload("wp1-dedup", [VOICE_SOURCE]))
        client.post("/approvals", json=_payload("wp1-dedup", [VOICE_SOURCE], gate="GATE_TECH_DESIGN_REVIEW"))
        data = client.get("/requirements/wp1-dedup/provenance").json()
        assert len(data["sources"]) == 1
        assert len(data["gates"]) == 2      # 两个 Gate 都记录了

    def test_gates_without_sources_are_reported(self, client):
        """供 E1 消费：哪个 Gate 还没有出处，一目了然。"""
        client.post("/approvals", json=_payload("wp1-mixed", [VOICE_SOURCE]))
        client.post("/approvals", json=_payload("wp1-mixed", None, gate="GATE_TECH_DESIGN_REVIEW"))
        data = client.get("/requirements/wp1-mixed/provenance").json()
        assert data["gates_without_sources"] == ["GATE_TECH_DESIGN_REVIEW"]
        assert len(data["sources"]) == 1

    def test_unknown_task_404(self, client):
        assert client.get("/requirements/wp1-nope/provenance").status_code == 404

    def test_tenant_isolation(self, client):
        client.post("/approvals", json=_payload("wp1-tenant", [VOICE_SOURCE]), headers={"X-Tenant": "line-a"})
        # 其它产品线查不到
        assert client.get("/requirements/wp1-tenant/provenance", headers={"X-Tenant": "line-b"}).status_code == 404
        # 本产品线查得到
        same = client.get("/requirements/wp1-tenant/provenance", headers={"X-Tenant": "line-a"})
        assert same.status_code == 200 and same.json()["sources"]
