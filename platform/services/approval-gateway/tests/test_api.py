"""审批网关 API 测试：状态机、三要素校验、决策留痕、超时策略。"""
import os
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

# 测试前切换到临时 SQLite，避免污染开发库
_tmpdir = tempfile.mkdtemp(prefix="approval-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _payload(**overrides) -> dict:
    from app.models import utcnow

    base = {
        "type": "GATE_REQUIREMENT_REVIEW",
        "stage": "产品域 G1",
        "requester": "产品Agent@issue-1234",
        "task_ref": "prodesgin#12",
        "context_snapshot": {
            "artifact_ref": "PRD v3",
            "diff_summary": "新增报表导出功能：支持按日期筛选，导出 CSV/Excel",
            "evidence": ["https://gitlab/.../prd-v3.md", "https://gitlab/.../issue-12"],
            "risk": {"score": 40, "level": "L2", "factors": ["影响面:纯新增", "不可逆:可回滚"]},
            "self_review": "验收标准已 EARS 化，边界情况已覆盖",
        },
        "options": ["approve", "reject", "revise"],
        "deadline": (utcnow() + timedelta(hours=24)).isoformat(),
        "escalation_policy": {
            "remind_after_hours": 24,
            "escalate_to": "team_lead",
            "timeout_action": "reject",
        },
    }
    base.update(overrides)
    return base


class TestLifecycle:
    def test_create_and_approve(self, client):
        r = client.post("/approvals", json=_payload())
        assert r.status_code == 201, r.text
        apv = r.json()
        assert apv["status"] == "pending"
        assert apv["context_snapshot"]["risk"]["level"] == "L2"

        d = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "approve", "decided_by": "产品负责人@Alice", "comment": ""},
        )
        assert d.status_code == 200
        assert d.json()["decision"] == "approve"

        apv = client.get(f"/approvals/{apv['id']}").json()
        assert apv["status"] == "approved"
        assert len(apv["decisions"]) == 1
        assert apv["decisions"][0]["decided_by"] == "产品负责人@Alice"

    def test_reject_without_reason_is_forbidden(self, client):
        apv = client.post("/approvals", json=_payload()).json()
        r = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "reject", "decided_by": "u1", "comment": ""},
        )
        assert r.status_code == 422  # 拒绝必须附原因（反馈闭环输入）

    def test_revise_then_resubmit(self, client):
        apv = client.post("/approvals", json=_payload()).json()

        r = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "revise", "decided_by": "产品负责人@Alice", "comment": "缺少导出权限控制说明"},
        )
        assert r.status_code == 200
        assert client.get(f"/approvals/{apv['id']}").json()["status"] == "revised"

        # 修订后重新提交：version+1，回到 pending，再批准
        r2 = client.post(f"/approvals/{apv['id']}/resubmit", json=_payload())
        assert r2.status_code == 200
        apv2 = r2.json()
        assert apv2["status"] == "pending" and apv2["version"] == 2

        r3 = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "approve", "decided_by": "产品负责人@Alice", "comment": "已补充权限说明"},
        )
        assert r3.status_code == 200

    def test_im_card_callback(self, client):
        apv = client.post("/approvals", json=_payload()).json()
        r = client.post(
            "/callbacks/im/card",
            json={"approval_id": apv["id"], "action": "approve", "operator": "产品负责人@Alice"},
        )
        assert r.status_code == 200
        assert client.get(f"/approvals/{apv['id']}").json()["status"] == "approved"

    def test_double_decision_conflict(self, client):
        apv = client.post("/approvals", json=_payload()).json()
        client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "approve", "decided_by": "u1"},
        )
        r = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "reject", "decided_by": "u2", "comment": "x"},
        )
        assert r.status_code == 409


class TestThreeElements:
    def test_missing_evidence_rejected(self, client):
        p = _payload()
        p["context_snapshot"]["evidence"] = []
        r = client.post("/approvals", json=p)
        assert r.status_code == 422  # 三要素缺一自检失败，不允许发出审批请求

    def test_missing_risk_rejected(self, client):
        p = _payload()
        del p["context_snapshot"]["risk"]
        r = client.post("/approvals", json=p)
        assert r.status_code == 422


class TestOverdue:
    def test_expired_approval_cannot_be_decided(self, client):
        from app.models import utcnow

        p = _payload(deadline=(utcnow() - timedelta(hours=1)).isoformat())
        apv = client.post("/approvals", json=p).json()
        r = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "approve", "decided_by": "u1"},
        )
        assert r.status_code == 410
