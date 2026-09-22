"""多产品线推广（Phase 3 ③）：信任按「产品线 × Gate」隔离 + 产品线级 SOP 覆盖。

要证明的事：
1. **新产品线从零开始积累信任**——不能继承老产品线已达成的 L1（否则新线直接无人值守，是重大风险）；
2. **惩罚不跨产品线扩散**——A 产品线的退回/否决不得把 B 产品线一起打成 L3；
3. **接口按 X-Tenant 隔离**，另有平台级总览 `/trust/overview`；
4. **产品线可独立调参**（`sop-config.d/<tenant>.yaml`），互不影响、基线不变。
"""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import sop_config
from app.db import Base
from app.models import ApprovalRequest, Decision, utcnow
from app.trust import all_trust, apply_penalty, compute_trust, split_scope, tenant_summary, trust_scope

GATE = "GATE_PR_REVIEW"
LINE_A = "line-a"
LINE_B = "line-b"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed(db, tenant: str, gate: str, approves: int = 0, revises: int = 0, rejects: int = 0):
    for kind, n in (("approve", approves), ("revise", revises), ("reject", rejects)):
        for _ in range(n):
            req = ApprovalRequest(
                type=gate, stage="研发域 G3", requester="dev-agent", tenant=tenant,
                context_snapshot={"artifact_ref": "mr!1", "diff_summary": "d", "evidence": ["e"],
                                  "risk": {"score": 1, "level": "L1", "factors": []}},
                deadline=utcnow() + timedelta(hours=1), escalation_policy={}, status="approved",
            )
            db.add(req)
            db.flush()
            db.add(Decision(request_id=req.id, decision=kind, decided_by="human", comment="c"))
    db.commit()


class TestTenantIsolation:
    def test_new_line_does_not_inherit_trust(self, db):
        """老产品线 12 次全票通过 → L1；新产品线同 Gate 零样本 → 必须 L3（强制人审）。"""
        _seed(db, LINE_A, GATE, approves=12)
        assert compute_trust(db, GATE, tenant=LINE_A)["level"] == "L1"

        fresh = compute_trust(db, GATE, tenant=LINE_B)
        assert fresh["level"] == "L3" and fresh["samples"] == 0
        assert "低于阈值" in fresh["reason"]

    def test_penalty_does_not_spill_across_lines(self, db):
        """A 产品线被否决 → 仅 A 的该 Gate 进惩罚期，B 不受影响。"""
        _seed(db, LINE_A, GATE, approves=12)
        _seed(db, LINE_B, GATE, approves=12)
        assert compute_trust(db, GATE, tenant=LINE_B)["level"] == "L1"

        apply_penalty(db, GATE, "退回：边界条件缺失", tenant=LINE_A)
        db.commit()

        a = compute_trust(db, GATE, tenant=LINE_A)
        b = compute_trust(db, GATE, tenant=LINE_B)
        assert a["level"] == "L3" and a["penalty_until"]
        assert b["level"] == "L1" and b["penalty_until"] is None

    def test_penalty_scoped_to_single_gate_within_line(self, db):
        other = "GATE_REQUIREMENT_REVIEW"
        _seed(db, LINE_A, GATE, approves=12)
        _seed(db, LINE_A, other, approves=12)
        apply_penalty(db, other, "否决：需求不可验收", tenant=LINE_A)
        db.commit()
        assert compute_trust(db, GATE, tenant=LINE_A)["level"] == "L1"
        assert compute_trust(db, other, tenant=LINE_A)["level"] == "L3"

    def test_scope_key_roundtrip(self):
        scope = trust_scope(LINE_A, GATE)
        assert scope == f"{LINE_A}::{GATE}"
        assert split_scope(scope) == (LINE_A, GATE)
        # 历史无前缀键归入默认租户（旧数据兼容）
        assert split_scope(GATE) == ("default", GATE)

    def test_all_trust_filtered_by_tenant(self, db):
        _seed(db, LINE_A, GATE, approves=12)
        _seed(db, LINE_B, GATE, rejects=2)
        only_b = all_trust(db, tenant=LINE_B)
        assert [t["gate"] for t in only_b] == [GATE]
        assert all(t["tenant"] == LINE_B for t in only_b)

        everything = all_trust(db)
        assert {(t["tenant"], t["gate"]) for t in everything} == {(LINE_A, GATE), (LINE_B, GATE)}

    def test_tenant_summary_overview(self, db):
        _seed(db, LINE_A, GATE, approves=12)
        _seed(db, LINE_B, GATE, rejects=3)
        summary = {row["tenant"]: row for row in tenant_summary(db)}
        assert summary[LINE_A]["levels"]["L1"] == 1 and summary[LINE_A]["auto_ratio"] == 1.0
        assert summary[LINE_B]["levels"]["L3"] == 1 and summary[LINE_B]["penalized"] == 0
        assert summary[LINE_A]["samples"] == 12 and summary[LINE_B]["samples"] == 3


class TestTenantSopOverlay:
    """产品线覆盖文件：`sop-config.d/<tenant>.yaml`（优先级：环境变量 > 覆盖 > 基线 > 默认）。"""

    @pytest.fixture()
    def isolated_config(self, tmp_path, monkeypatch):
        base = tmp_path / "sop-config.yaml"
        base.write_text(
            "version: 9\ntrust:\n  min_samples: 10\n  l1_approve_rate: 0.9\n  sample_ratio: 0.3\n"
            "gates:\n  GATE_PR_REVIEW:\n    sla_hours: 24\n",
            encoding="utf-8",
        )
        overlay = tmp_path / "sop-config.d"
        overlay.mkdir()
        (overlay / f"{LINE_B}.yaml").write_text(
            "trust:\n  min_samples: 5\n  l1_approve_rate: 0.95\n  sample_ratio: 0.5\n"
            "gates:\n  GATE_PR_REVIEW:\n    sla_hours: 4\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sop_config, "_candidates", [base])
        monkeypatch.setattr(sop_config, "_overlay_dir", overlay)
        monkeypatch.setattr(sop_config, "_cache", {})
        monkeypatch.delenv("TRUST_SAMPLE_RATIO", raising=False)
        yield
        sop_config._cache.clear()

    def test_overlay_applies_only_to_its_line(self, isolated_config):
        assert sop_config.trust_setting("min_samples", cast=int, tenant=LINE_A) == 10
        assert sop_config.trust_setting("min_samples", cast=int, tenant=LINE_B) == 5
        assert sop_config.trust_setting("l1_approve_rate", tenant=LINE_A) == 0.9
        assert sop_config.trust_setting("l1_approve_rate", tenant=LINE_B) == 0.95
        # 未覆盖项沿用基线
        assert sop_config.gate_sla_hours("GATE_PR_REVIEW", tenant=LINE_A) == 24
        assert sop_config.gate_sla_hours("GATE_PR_REVIEW", tenant=LINE_B) == 4

    def test_overlay_changes_trust_outcome(self, db, isolated_config):
        """同样 6 次全票通过：基线需 10 样本 → L2；B 线阈值 5 → L1。"""
        _seed(db, LINE_A, GATE, approves=6)
        _seed(db, LINE_B, GATE, approves=6)
        assert compute_trust(db, GATE, tenant=LINE_A)["level"] == "L2"
        assert compute_trust(db, GATE, tenant=LINE_B)["level"] == "L1"

    def test_overlay_hot_reload(self, isolated_config, tmp_path):
        """覆盖文件改动后无需重启即生效（mtime 热更新）。"""
        assert sop_config.trust_setting("min_samples", cast=int, tenant=LINE_B) == 5
        path = sop_config.overlay_path(LINE_B)
        path.write_text("trust:\n  min_samples: 3\n", encoding="utf-8")
        import os
        import time

        os.utime(path, (time.time() + 2, time.time() + 2))
        assert sop_config.trust_setting("min_samples", cast=int, tenant=LINE_B) == 3
        # 基线不受影响
        assert sop_config.trust_setting("min_samples", cast=int, tenant=LINE_A) == 10

    def test_missing_overlay_falls_back_to_base(self, isolated_config):
        assert sop_config.overlay_path("line-c") is None
        assert sop_config.trust_setting("min_samples", cast=int, tenant="line-c") == 10


class TestTenantApiIsolation:
    """API 级：X-Tenant 头决定读写与信任视图的隔离边界。"""

    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient

        from test_api import _payload  # noqa: F401  确保 DATABASE_URL 已就绪

        from app.main import app

        with TestClient(app) as c:
            yield c

    def _make(self, client, tenant: str, gate: str):
        payload = {
            "type": gate,
            "stage": "研发域 G3",
            "requester": "dev-agent",
            "task_ref": f"{tenant}-task",
            "context_snapshot": {
                "artifact_ref": "mr!9",
                "diff_summary": "产品线隔离验证",
                "evidence": ["e1"],
                "risk": {"score": 10, "level": "L1", "factors": ["常规"]},
            },
            "deadline": (utcnow() + timedelta(hours=1)).isoformat(),
            "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
        }
        return client.post("/approvals", json=payload, headers={"X-Tenant": tenant}).json()

    def test_approvals_isolated_by_tenant_header(self, client):
        apv_a = self._make(client, LINE_A, GATE)
        apv_b = self._make(client, LINE_B, GATE)
        listed_a = client.get("/approvals", headers={"X-Tenant": LINE_A}).json()
        # 验证隔离**属性**（而不是"列表恰好一项"——同租户的其它用例也会建单）：
        # 本租户能列出自己刚建的单，且列表里绝不出现其它租户的单。
        ids_a = [x["id"] for x in listed_a]
        assert apv_a["id"] in ids_a
        assert apv_b["id"] not in ids_a
        assert all(x["tenant"] == LINE_A for x in listed_a)
        # 跨产品线直读必须 404（隔离而非仅列表过滤）
        assert client.get(f"/approvals/{apv_a['id']}", headers={"X-Tenant": LINE_B}).status_code == 404
        # 决策接口同样隔离
        resp = client.post(
            f"/approvals/{apv_a['id']}/decision",
            json={"decision": "approve", "decided_by": "评审员"},
            headers={"X-Tenant": LINE_B},
        )
        assert resp.status_code == 404
        assert self._make(client, LINE_A, GATE)["tenant"] == LINE_A
        assert apv_b["tenant"] == LINE_B

    def test_trust_endpoints_respect_tenant(self, client):
        self._make(client, LINE_A, GATE)
        one_a = client.get(f"/trust/{GATE}", headers={"X-Tenant": LINE_A}).json()
        one_b = client.get(f"/trust/{GATE}", headers={"X-Tenant": LINE_B}).json()
        assert one_a["tenant"] == LINE_A and one_b["tenant"] == LINE_B
        assert one_a["scope"] == f"{LINE_A}::{GATE}"

        overview = client.get("/trust/overview").json()
        tenants = {row["tenant"] for row in overview}
        assert {LINE_A, LINE_B} <= tenants
        assert all({"tenant", "gates", "levels", "auto_ratio"} <= set(row) for row in overview)
