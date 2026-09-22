"""鉴权专项回归（approval-gateway）：服务令牌 + 决策/管理动作的角色校验。

两个实测问题：
  ① `AUTH_MODE=oidc` 只校验"登录了没有"，不校验"是谁"：
     任何登录用户（含只读账号）都能 `POST /approvals/{id}/decision` 批准审批、
     也能 `POST /admin/sweep` 触发**跨全部租户**的超时自动处置；
  ② 服务间无令牌：与其它服务一样，端口可达即等于有全部写权限。
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="gw-authz-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/gw.db"

from app.auth import refresh_config  # noqa: E402
from app.main import app  # noqa: E402
from app.models import utcnow  # noqa: E402

BODY = {
    "type": "GATE_REQUIREMENT_REVIEW", "stage": "authz", "requester": "r", "task_ref": "authz-1",
    "context_snapshot": {"artifact_ref": "a", "diff_summary": "d", "evidence": ["e"],
                         "risk": {"score": 10, "level": "L2", "factors": []}},
    "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
    refresh_config()


def _approval(client) -> dict:
    r = client.post("/approvals", json={**BODY, "deadline": (utcnow() + timedelta(hours=2)).isoformat()})
    assert r.status_code == 201, r.text
    return r.json()


class TestServiceToken:
    def test_write_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/approvals", json={**BODY, "deadline": (utcnow() + timedelta(hours=2)).isoformat()})
        assert r.status_code == 401, r.text

    def test_health_and_im_callback_are_exempt(self, client):
        """`SERVICE_AUTH_MODE` 打开时，/health 与 IM 回调（自带飞书签名）仍可到达。"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("SERVICE_AUTH_MODE", "token")
            mp.setenv("SERVICE_AUTH_TOKEN", "svc")
            assert client.get("/health").status_code == 200
            # IM 回调走自己的签名校验（未配置密钥 → 503，而不是中间件的 401）
            apv = _approval_with_token(client)
            r = client.post("/callbacks/im/card", json={"approval_id": apv["id"], "action": "approve",
                                                        "operator": "x", "comment": "c"})
            assert r.status_code != 401, r.text


def _approval_with_token(client) -> dict:
    r = client.post("/approvals", json={**BODY, "deadline": (utcnow() + timedelta(hours=2)).isoformat()},
                    headers={"X-Service-Token": "svc"})
    assert r.status_code == 201, r.text
    return r.json()


class TestApproverRoleRequired:
    """角色校验：oden 模式下只读账号不得批准、不得触发全局扫描。"""

    def test_decision_forbidden_for_viewer_role(self, client, monkeypatch):
        apv = _approval(client)
        monkeypatch.setenv("AUTH_MODE", "oidc")
        monkeypatch.setenv("APPROVER_ROLES", "approver,admin")
        refresh_config()
        from app import auth as auth_mod

        monkeypatch.setattr(auth_mod, "verify_bearer",
                            lambda token: auth_mod.Principal(sub="viewer-1", roles=["viewer"]))
        r = client.post(f"/approvals/{apv['id']}/decision",
                        json={"decision": "approve", "comment": "越权", "decided_by": "viewer-1"},
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code == 403, r.text
        assert "需要角色" in r.json()["detail"]

    def test_decision_allowed_for_approver_role(self, client, monkeypatch):
        apv = _approval(client)
        monkeypatch.setenv("AUTH_MODE", "oidc")
        refresh_config()
        from app import auth as auth_mod

        monkeypatch.setattr(auth_mod, "verify_bearer",
                            lambda token: auth_mod.Principal(sub="approver-1", roles=["approver"]))
        r = client.post(f"/approvals/{apv['id']}/decision",
                        json={"decision": "approve", "comment": "同意", "decided_by": "自称的名字"},
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200, r.text
        # 署名必须是登录主体，而不是客户端填的名字（否则审计可被冒名）
        assert r.json()["decided_by"] == "approver-1"

    def test_admin_sweep_forbidden_for_viewer_role(self, client, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "oidc")
        refresh_config()
        from app import auth as auth_mod

        monkeypatch.setattr(auth_mod, "verify_bearer",
                            lambda token: auth_mod.Principal(sub="viewer-1", roles=["viewer"]))
        r = client.post("/admin/sweep", headers={"Authorization": "Bearer fake"})
        assert r.status_code == 403, r.text

    def test_off_mode_still_open_for_local_dev(self, client, monkeypatch):
        """AUTH_MODE=off（本机开发）行为不变：不因为加了角色校验而挡住本地流程。"""
        apv = _approval(client)
        r = client.post(f"/approvals/{apv['id']}/decision", json={"decision": "approve", "comment": "本地", "decided_by": "本地值班"})
        assert r.status_code == 200, r.text
