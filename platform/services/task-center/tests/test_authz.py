"""鉴权专项回归：入站 webhook 签名 + 服务间令牌中间件。

背景（实测）：清点 7 个服务 33 个写接口，**22 个没有任何鉴权依赖**；其中
`POST /alerts/prometheus` 连"看着像有鉴权"的假象都没有——`demo_alert.py` 里那个
`WEBHOOK_SECRET` 只给了 gitlab-bridge，task-center 根本没读，任何能连上端口的人都能
凭空造告警 → 建运维任务 → 自动拉起运维域 SOP。

本文件测三件事：
  ① 无凭据/错凭据的告警 webhook 被拒，且**没有建任何任务**（拒绝要拒绝干净）；
  ② 密钥未配置时 fail-closed（503），而不是"没配就是全开"；
  ③ `SERVICE_AUTH_MODE=token` 时，无 `X-Service-Token` 的写请求 401，带对的通过，
     豁免路径（/health）与 `/alerts/prometheus` 的自带签名通道不受影响。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='authz-')}/tc.db"
os.environ["ALERT_WEBHOOK_SECRET"] = "authz-secret"

from app.main import app  # noqa: E402

ALERT = {"status": "firing",
         "alerts": [{"status": "firing", "labels": {"alertname": "A", "fingerprint": "fp1"}}]}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _alert_count(client) -> int:
    return len([t for t in client.get("/tasks").json() if t["sop"] == "ops-domain/alert-to-postmortem"])


class TestAlertWebhookAuth:
    def test_missing_credential_rejected_and_no_task_created(self, client):
        before = _alert_count(client)
        r = client.post("/alerts/prometheus", json=ALERT)
        assert r.status_code == 401, r.text
        assert _alert_count(client) == before, "鉴权失败却建了任务"

    def test_wrong_token_rejected(self, client):
        r = client.post("/alerts/prometheus", json=ALERT, headers={"X-Webhook-Token": "wrong"})
        assert r.status_code == 401

    def test_bad_signature_rejected(self, client):
        r = client.post("/alerts/prometheus", json=ALERT,
                        headers={"X-Webhook-Signature": "sha256=deadbeef"})
        assert r.status_code == 401

    def test_valid_token_accepted(self, client):
        before = _alert_count(client)
        r = client.post("/alerts/prometheus", json=ALERT,
                        headers={"X-Webhook-Token": "authz-secret"})
        assert r.status_code == 200 and len(r.json()["tasks"]) == 1
        assert _alert_count(client) == before + 1

    def test_valid_signature_accepted(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_RUNTIME_URL", "")   # 不真的去拉起运维 SOP
        import json

        raw = json.dumps(ALERT).encode()
        sig = hmac.new(b"authz-secret", raw, hashlib.sha256).hexdigest()
        r = client.post("/alerts/prometheus", content=raw,
                        headers={"Content-Type": "application/json",
                                 "X-Webhook-Signature": f"sha256={sig}"})
        assert r.status_code == 200, r.text

    def test_no_key_configured_fails_closed(self, client, monkeypatch):
        monkeypatch.delenv("ALERT_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
        r = client.post("/alerts/prometheus", json=ALERT)
        assert r.status_code == 503, r.text
        assert "未配置密钥" in r.json()["detail"]


class TestServiceTokenMiddleware:
    def test_write_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc-token")
        r = client.post("/tasks", json={"sop": "dev-domain/issue-to-merge"})
        assert r.status_code == 401, r.text

    def test_correct_token_passes(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc-token")
        r = client.post("/tasks", json={"sop": "dev-domain/issue-to-merge"},
                        headers={"X-Service-Token": "svc-token"})
        assert r.status_code == 201, r.text

    def test_health_is_exempt(self, client, monkeypatch):
        """免鉴权路径不能被裸前缀豁免误伤：/health 放行，/health-admin 不在此列。"""
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc-token")
        assert client.get("/health").status_code == 200
        assert client.get("/healthz-admin").status_code == 401

    def test_alert_webhook_still_uses_its_own_signature(self, client, monkeypatch):
        """服务令牌模式下，Alertmanager 自带签名即可通过（豁免生效）。"""
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc-token")
        r = client.post("/alerts/prometheus", json=ALERT, headers={"X-Webhook-Token": "authz-secret"})
        assert r.status_code == 200, r.text

    def test_proxy_user_header_not_trusted_by_default(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc-token")
        monkeypatch.delenv("TRUST_PROXY_AUTH", raising=False)
        r = client.post("/tasks", json={"sop": "dev-domain/issue-to-merge"},
                        headers={"X-Platform-User": "ceo"})
        assert r.status_code == 401, r.text
