"""回归测试：camp-pst-20260922 全系统测试发现的缺陷（防复发）。

每条对应报告中的一个 BUG，测试名标注编号，便于与缺陷台账对照。
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="camp-regression-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/tc.db"

from app.main import app  # noqa: E402
from app.state import TRANSITIONS, validate_transition  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _task(client, tenant="default", **over):
    body = {"sop": "dev-domain/issue-to-merge", **over}
    return client.post("/tasks", json=body, headers={"X-Tenant": tenant})


class TestBug005ApprovalWaitCannotBeBypassed:
    def test_waiting_approval_to_running_requires_decision(self, client):
        t = _task(client).json()
        client.post(f"/tasks/{t['id']}/transition", json={"to": "running", "by": "agent"})
        client.post(f"/tasks/{t['id']}/transition", json={"to": "waiting_approval", "by": "agent"})
        r = client.post(f"/tasks/{t['id']}/transition", json={"to": "running", "by": "agent"})
        assert r.status_code == 409 and "决策依据" in r.json()["detail"]

    def test_with_decision_evidence_it_is_allowed(self, client):
        t = _task(client).json()
        client.post(f"/tasks/{t['id']}/transition", json={"to": "running", "by": "agent"})
        client.post(f"/tasks/{t['id']}/transition", json={"to": "waiting_approval", "by": "agent"})
        r = client.post(f"/tasks/{t['id']}/transition",
                        json={"to": "running", "by": "human", "detail": {"approval_id": "apv_x", "decided_by": "李经理"}})
        assert r.status_code == 200 and r.json()["status"] == "running"

    def test_reject_exit_also_needs_evidence(self, client):
        t = _task(client).json()
        client.post(f"/tasks/{t['id']}/transition", json={"to": "running", "by": "a"})
        client.post(f"/tasks/{t['id']}/transition", json={"to": "waiting_approval", "by": "a"})
        assert client.post(f"/tasks/{t['id']}/transition", json={"to": "rejected", "by": "a"}).status_code == 409

    def test_state_machine_still_allows_normal_flow(self):
        assert validate_transition("pending", "running")
        assert validate_transition("waiting_approval", "running")
        assert "waiting_approval" in TRANSITIONS


class TestBug006TenantScope:
    def test_detail_is_tenant_scoped(self, client):
        t = _task(client, tenant="line-a").json()
        assert client.get(f"/tasks/{t['id']}", headers={"X-Tenant": "line-a"}).status_code == 200
        assert client.get(f"/tasks/{t['id']}", headers={"X-Tenant": "line-b"}).status_code == 404

    def test_transition_and_timeline_are_tenant_scoped(self, client):
        t = _task(client, tenant="line-a").json()
        assert client.post(f"/tasks/{t['id']}/transition", json={"to": "running", "by": "x"},
                           headers={"X-Tenant": "line-b"}).status_code == 404
        assert client.post(f"/tasks/{t['id']}/events", json={"event_type": "x", "by": "x"},
                           headers={"X-Tenant": "line-b"}).status_code == 404


class TestBug015ExternalRef:
    def test_repeated_empty_external_ref_is_not_500(self, client):
        assert _task(client).status_code == 201
        assert _task(client).status_code == 201        # 空引用存 NULL，不撞唯一索引

    def test_duplicate_non_empty_external_ref_is_idempotent(self, client):
        first = _task(client, external_ref="proj#1").json()
        second = _task(client, external_ref="proj#1").json()
        assert first["id"] == second["id"]             # 幂等：同一引用只建一个任务

    def test_events_reject_empty_ref(self, client):
        r = client.post("/events", json={"event_id": "e-empty", "event_type": "gitlab.issue.opened",
                                         "source": "t", "payload": {"project_id": "", "iid": ""}})
        assert r.status_code == 422

    def test_events_ambiguous_ref_is_refused(self, client):
        _task(client, tenant="line-a", external_ref="dup#9")
        _task(client, tenant="line-b", external_ref="dup#9")
        r = client.post("/events", json={"event_id": "e-amb", "event_type": "approval.requested",
                                         "source": "t", "payload": {"task_ref": "dup#9"}})
        assert r.status_code == 200 and r.json()["status"] == "ambiguous_ref"
