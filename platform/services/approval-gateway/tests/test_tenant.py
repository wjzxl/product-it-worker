"""多租户隔离测试：审批按租户隔离，跨租户不可见/不可操作。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import test_api  # noqa: F401  确保 DATABASE_URL 就绪

    from app.main import app

    with TestClient(app) as c:
        yield c


def _payload(requester: str = "agent@acme"):
    from test_api import _payload as base

    p = base()
    p["requester"] = requester
    return p


class TestTenantIsolation:
    def test_create_and_list_per_tenant(self, client):
        a = client.post("/approvals", json=_payload("agent@acme"), headers={"X-Tenant": "acme"}).json()
        b = client.post("/approvals", json=_payload("agent@globex"), headers={"X-Tenant": "globex"}).json()
        assert a["tenant"] == "acme" and b["tenant"] == "globex"

        acme = client.get("/approvals", headers={"X-Tenant": "acme"}).json()
        globex = client.get("/approvals", headers={"X-Tenant": "globex"}).json()
        assert all(x["tenant"] == "acme" for x in acme) and any(x["id"] == a["id"] for x in acme)
        assert all(x["tenant"] == "globex" for x in globex) and any(x["id"] == b["id"] for x in globex)
        assert not any(x["id"] == b["id"] for x in acme)  # 互相不可见

    def test_cross_tenant_access_404(self, client):
        apv = client.post("/approvals", json=_payload(), headers={"X-Tenant": "acme"}).json()
        assert client.get(f"/approvals/{apv['id']}", headers={"X-Tenant": "globex"}).status_code == 404
        assert client.get(f"/approvals/{apv['id']}", headers={"X-Tenant": "acme"}).status_code == 200

    def test_cross_tenant_decision_blocked(self, client):
        apv = client.post("/approvals", json=_payload(), headers={"X-Tenant": "acme"}).json()
        r = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "approve", "decided_by": "attacker@globex"},
            headers={"X-Tenant": "globex"},
        )
        assert r.status_code == 404  # 跨租户不可操作
        # 本租户仍可正常决策
        r = client.post(
            f"/approvals/{apv['id']}/decision",
            json={"decision": "approve", "decided_by": "approver@acme"},
            headers={"X-Tenant": "acme"},
        )
        assert r.status_code == 200

    def test_default_tenant_without_header(self, client):
        apv = client.post("/approvals", json=_payload()).json()
        assert apv["tenant"] == "default"
