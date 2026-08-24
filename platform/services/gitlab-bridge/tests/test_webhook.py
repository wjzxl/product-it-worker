"""Webhook 端点测试：签名校验、事件发布、未知事件忽略。"""
import logging

from fastapi.testclient import TestClient

from app.main import Settings, create_app

ISSUE_OPEN = {
    "object_kind": "issue",
    "project": {"id": 42, "path_with_namespace": "sandbox/prodesgin"},
    "object_attributes": {
        "iid": 12, "title": "报表导出功能", "description": "", "state": "opened",
        "action": "open", "labels": [],
    },
}


def _client() -> TestClient:
    app = create_app(Settings(webhook_secret="s3cret"))  # 无 publish_targets → LogPublisher
    return TestClient(app)


def test_valid_webhook_publishes_event(caplog):
    caplog.set_level(logging.INFO)
    r = _client().post(
        "/webhooks/gitlab",
        json=ISSUE_OPEN,
        headers={"X-Gitlab-Token": "s3cret", "X-Gitlab-Event-UUID": "uuid-222"},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "accepted", "events": ["gitlab.issue.opened"]}
    assert "gitlab.issue.opened" in caplog.text  # LogPublisher 已发布


def test_invalid_token_rejected():
    r = _client().post("/webhooks/gitlab", json=ISSUE_OPEN, headers={"X-Gitlab-Token": "wrong"})
    assert r.status_code == 401


def test_missing_token_rejected():
    r = _client().post("/webhooks/gitlab", json=ISSUE_OPEN)
    assert r.status_code == 401


def test_unknown_event_accepted_but_ignored():
    r = _client().post(
        "/webhooks/gitlab",
        json={"object_kind": "note", "object_attributes": {}},
        headers={"X-Gitlab-Token": "s3cret"},
    )
    assert r.status_code == 200
    assert r.json()["events"] == []


def test_health():
    assert _client().get("/health").json()["service"] == "gitlab-bridge"
