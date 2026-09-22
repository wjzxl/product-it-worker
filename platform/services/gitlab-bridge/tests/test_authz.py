"""鉴权专项回归（gitlab-bridge）：出站写接口需服务令牌，入站 webhook 仍用自己的签名。

清点结论：`/gitlab/*`（建 Issue、建/合并 MR、评论、触发流水线、注册 hook）原本无鉴权——
任何能连上端口的人都能用平台持有的 GitLab Token 去改代码仓库。
`/webhooks/gitlab` 走 `X-Gitlab-Token`/HMAC 自查，故对服务令牌豁免（豁免必须"自带签名"才成立）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app

ISSUE_OPEN = {
    "object_kind": "issue",
    "project": {"id": 42, "path_with_namespace": "sandbox/prodesgin"},
    "object_attributes": {"iid": 12, "title": "报表导出功能", "description": "", "state": "opened",
                          "action": "open", "labels": []},
}


@pytest.fixture()
def client():
    with TestClient(create_app(Settings(webhook_secret="s3cret"))) as c:
        yield c


class TestServiceToken:
    def test_gitlab_write_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/gitlab/issues", json={"project_id": 42, "title": "x"})
        assert r.status_code == 401, r.text

    def test_correct_token_passes_middleware(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/gitlab/issues", json={"project_id": 42, "title": "x"},
                        headers={"X-Service-Token": "svc"})
        assert r.status_code != 401, r.text      # 业务层可能 502（无真实 GitLab），但不是鉴权拒绝

    def test_webhook_exempt_but_still_verifies_its_own_signature(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        # 豁免服务令牌，但 GitLab 自己的 token 校验必须仍然生效
        assert client.post("/webhooks/gitlab", json=ISSUE_OPEN).status_code == 401
        ok = client.post("/webhooks/gitlab", json=ISSUE_OPEN,
                         headers={"X-Gitlab-Token": "s3cret", "X-Gitlab-Event-UUID": "u1"})
        assert ok.status_code == 200, ok.text

    def test_health_exempt(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.get("/health").status_code == 200
