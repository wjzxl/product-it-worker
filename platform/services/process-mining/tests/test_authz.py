"""鉴权专项回归（process-mining）：服务令牌 + G7 审批校验收紧。

清点结论：`/mining/proposals`、`/mining/proposals/submit`、`/mining/proposals/apply` 原本无鉴权——
最后一个是"改 SOP 配置"的动作（会写配置中心并登记 GitLab 改进项）。
`apply` 本身已校验 G7 审批状态（本文件的用例守住这条底线不被改回去）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


class TestServiceToken:
    def test_apply_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/mining/proposals/apply",
                        json={"approval_id": "apv_x", "proposal": {"action": {"type": "noop"}}})
        assert r.status_code == 401, r.text

    def test_proposals_require_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.post("/mining/proposals", json={}).status_code == 401

    def test_correct_token_passes_middleware(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/mining/proposals", json={}, headers={"X-Service-Token": "svc"})
        assert r.status_code != 401, r.text

    def test_health_exempt(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.get("/health").status_code == 200


class TestG7GateCannotBeSkipped:
    def test_apply_without_approved_gate_is_refused(self, client, monkeypatch):
        """审批网关不可达/未获批时必须拒绝应用（拿不到证据就不改配置）。"""
        monkeypatch.setenv("APPROVAL_GATEWAY_URL", "http://127.0.0.1:1")
        r = client.post("/mining/proposals/apply",
                        json={"approval_id": "apv_x", "proposal": {"action": {"type": "noop"}}})
        assert r.status_code in (409, 502), r.text
