"""信任分级动态升降级测试（Phase 3）。

- 单元级：独立内存 DB 验证指标→等级映射、惩罚期、抽样器确定性；
- API 级：共享应用验证 auto_gate 自动放行、否决后强制 L3。
"""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ApprovalRequest, Decision, TrustState, utcnow
from app.trust import all_trust, apply_penalty, compute_trust, should_auto_approve

SCOPE = "GATE_POSTMORTEM_REVIEW"
PENALTY_SCOPE = "GATE_CHANGE_APPROVAL"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _seed(db, scope: str, approves: int = 0, revises: int = 0, rejects: int = 0):
    for kind, n in (("approve", approves), ("revise", revises), ("reject", rejects)):
        for i in range(n):
            req = ApprovalRequest(
                type=scope, stage="测试", requester="test-agent",
                context_snapshot={"artifact_ref": "x", "diff_summary": "d", "evidence": ["e"],
                                  "risk": {"score": 1, "level": "L2", "factors": []}},
                deadline=utcnow() + timedelta(hours=1), escalation_policy={}, status="approved",
            )
            db.add(req)
            db.flush()
            db.add(Decision(request_id=req.id, decision=kind, decided_by="human", comment="c"))
    db.commit()


class TestTrustMetrics:
    def test_l1_when_high_approve_rate(self, db):
        _seed(db, SCOPE, approves=12)
        t = compute_trust(db, SCOPE)
        assert t["level"] == "L1" and t["approve_rate"] == 1.0 and t["sample_ratio"] == 0.0

    def test_l2_when_moderate(self, db):
        _seed(db, SCOPE, approves=9, revises=2)  # 通过率 81.8%
        t = compute_trust(db, SCOPE)
        assert t["level"] == "L2" and t["sample_ratio"] > 0

    def test_l3_when_low(self, db):
        _seed(db, SCOPE, approves=5, rejects=5)  # 50%
        assert compute_trust(db, SCOPE)["level"] == "L3"

    def test_insufficient_samples_not_l1(self, db):
        _seed(db, SCOPE, approves=3)
        assert compute_trust(db, SCOPE)["level"] == "L2"  # 样本不足，最高 L2

    def test_recent_revise_blocks_l1(self, db):
        _seed(db, SCOPE, approves=12)
        _seed(db, SCOPE, revises=1)  # 最新一条是退回
        t = compute_trust(db, SCOPE)
        assert t["level"] in ("L2", "L3") and t["revises"] == 1

    def test_penalty_forces_l3_then_recovers(self, db):
        _seed(db, SCOPE, approves=12)
        assert compute_trust(db, SCOPE)["level"] == "L1"
        until = apply_penalty(db, SCOPE, "否决：范围超纲")
        db.commit()
        t = compute_trust(db, SCOPE)
        assert t["level"] == "L3" and t["penalty_until"]
        # 惩罚到期后按指标重新计算（指标仍达标 → 恢复 L1）
        t_after = compute_trust(db, SCOPE, now=until + timedelta(seconds=1))
        assert t_after["level"] == "L1" and t_after["penalty_until"] is None

    def test_penalty_isolated_per_scope(self, db):
        _seed(db, SCOPE, approves=12)
        apply_penalty(db, PENALTY_SCOPE, "其它 Gate 的否决")
        db.commit()
        assert compute_trust(db, SCOPE)["level"] == "L1"           # 不受影响
        assert compute_trust(db, PENALTY_SCOPE)["level"] == "L3"   # 惩罚生效

    def test_all_trust_lists_scopes(self, db):
        _seed(db, SCOPE, approves=12)
        _seed(db, PENALTY_SCOPE, approves=2, rejects=2)
        scopes = {t["gate"]: t["level"] for t in all_trust(db)}
        assert scopes[SCOPE] == "L1" and scopes[PENALTY_SCOPE] == "L3"
        assert all(t["tenant"] == "default" for t in all_trust(db))


class TestSampler:
    def test_l1_always_auto(self):
        ok, why = should_auto_approve({"level": "L1"}, "apv_1")
        assert ok and "L1" in why

    def test_l3_never_auto(self):
        ok, why = should_auto_approve({"level": "L3"}, "apv_1")
        assert not ok and "L3" in why

    def test_l2_sampling_deterministic(self):
        trust = {"level": "L2", "sample_ratio": 0.5}
        first = should_auto_approve(trust, "apv_fixed")
        assert first == should_auto_approve(trust, "apv_fixed")  # 同一 token 结果一致

    def test_l2_ratio_bounds(self):
        assert should_auto_approve({"level": "L2", "sample_ratio": 1.0}, "x")[0] is True
        assert should_auto_approve({"level": "L2", "sample_ratio": 0.0}, "x")[0] is False


class TestApiAutoGate:
    def test_l1_auto_approve_and_penalty_downgrade(self, client):
        """API 级：连续人工批准使该 Gate 升到 L1 → auto_gate 自动放行；否决后强制 L3。"""
        payload = {
            "type": SCOPE,
            "stage": "测试域 G6",
            "requester": "运维Agent@test",
            "task_ref": "trust-test",
            "context_snapshot": {
                "artifact_ref": "复盘-x",
                "diff_summary": "自动放行演练",
                "evidence": ["evidence-1"],
                "risk": {"score": 30, "level": "L2", "factors": ["常规"]},
            },
            "options": ["approve", "reject", "revise"],
            "deadline": (utcnow() + timedelta(hours=1)).isoformat(),
            "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
        }
        for _ in range(10):
            apv = client.post("/approvals", json=payload).json()
            client.post(f"/approvals/{apv['id']}/decision",
                        json={"decision": "approve", "decided_by": "复盘审查员"})

        trust = client.get(f"/trust/{SCOPE}").json()
        assert trust["level"] == "L1" and trust["samples"] >= 10

        auto = client.post("/approvals", json={**payload, "auto_gate": True}).json()
        assert auto["auto_decision"] == "approved" and auto["status"] == "approved"
        assert auto["trust_level"] == "L1"
        detail = client.get(f"/approvals/{auto['id']}").json()
        assert detail["decisions"][-1]["decided_by"] == "system:trust"
        assert "自动放行" in detail["decisions"][-1]["comment"]

        # 真实否决 → 惩罚期 → 强制 L3，auto_gate 不再自动放行
        rejected = client.post("/approvals", json=payload).json()
        client.post(f"/approvals/{rejected['id']}/decision",
                    json={"decision": "reject", "decided_by": "复盘审查员", "comment": "复盘结论不充分"})
        trust_after = client.get(f"/trust/{SCOPE}").json()
        assert trust_after["level"] == "L3" and trust_after["penalty_until"]

        blocked = client.post("/approvals", json={**payload, "auto_gate": True}).json()
        assert blocked["auto_decision"] == "pending" and blocked["status"] == "pending"

    def test_trust_endpoints(self, client):
        assert isinstance(client.get("/trust").json(), list)
        one = client.get(f"/trust/{SCOPE}").json()
        assert one["gate"] == SCOPE and one["tenant"] == "default"
        assert one["scope"] == f"default::{SCOPE}"  # 作用域键 = 产品线 × Gate


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from test_api import _payload  # noqa: F401  确保 DATABASE_URL 已就绪

    from app.main import app

    with TestClient(app) as c:
        yield c
