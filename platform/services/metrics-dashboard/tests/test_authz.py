"""鉴权/注入专项回归（metrics-dashboard）：身份透传、服务令牌、驾驶舱 XSS。

三条实测问题：
  ① `/console/decide`、`/console/feedback` 转调审批网关时**不带任何身份头**：
     `AUTH_MODE=oidc` 下网关必然 401（驾驶舱的"批准"按钮永远点不动），
     放行模式下网关也无从记录"是谁批的"；
  ② 驾驶舱把上游数据（标题、需求文本、阶段名——都可被用户左右）直接拼进 HTML = 存储型 XSS；
  ③ 服务间无鉴权（`POST /console/decide` 谁都能替人做决策）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APPROVAL_GATEWAY_URL", "http://127.0.0.1:1")  # 故意打不通，只看是否发起/被拒

from app import console  # noqa: E402
from app.main import app, esc  # noqa: E402


def _ci(headers: dict, name: str) -> str:
    """HTTP 头大小写不敏感（Starlette 按收到的大小写原样给出）。"""
    return {k.lower(): v for k, v in headers.items()}.get(name.lower(), "")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


class TestIdentityForwarding:
    def test_forward_headers_keeps_only_identity_and_tenant(self):
        raw = {"Authorization": "Bearer abc", "X-Tenant": "line-a", "X-Service-Token": "s",
               "Cookie": "sid=1", "Host": "evil", "Content-Length": "10", "X-Random": "x"}
        got = console.forward_headers(raw)
        assert got == {"Authorization": "Bearer abc", "X-Tenant": "line-a",
                       "X-Service-Token": "s", "Cookie": "sid=1"}

    def test_decide_forwards_authorization_to_gateway(self, client, monkeypatch):
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers or {}
            raise RuntimeError("stop here")

        monkeypatch.setattr("app.console.httpx.post", fake_post)
        r = client.post("/console/decide", json={"approval_id": "apv_1", "decision": "approve",
                                                 "comment": "同意", "decided_by": "驾驶舱"},
                        headers={"Authorization": "Bearer user-token", "X-Tenant": "line-a"})
        assert r.status_code == 502                     # 假 httpx 抛错 → 502，但请求已发出
        assert _ci(seen["headers"], "Authorization") == "Bearer user-token"
        assert _ci(seen["headers"], "X-Tenant") == "line-a"
        assert seen["url"].endswith("/approvals/apv_1/decision")

    def test_feedback_forwards_authorization(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr("app.console.httpx.post",
                            lambda url, json=None, headers=None, timeout=None: seen.update(
                                url=url, headers=headers) or (_ for _ in ()).throw(RuntimeError("stop")))
        r = client.post("/console/feedback", json={"check_id": "gck_1", "verdict": "false_positive",
                                                   "note": "其实可放行", "by": "驾驶舱"},
                        headers={"Authorization": "Bearer user-token"})
        assert r.status_code == 502
        assert _ci(seen["headers"], "Authorization") == "Bearer user-token"


class TestDashboardEscaping:
    def test_esc_escapes_html_and_quotes(self):
        assert esc("<script>alert('x')</script>") == "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;"
        assert esc(None) == ""

    def test_malicious_task_title_is_escaped_in_page(self, client, monkeypatch):
        payload = "<img src=x onerror=alert(1)>"
        monkeypatch.setattr("app.main.console.build_queue", lambda *a, **k: {
            "queue": [{"kind": "approval", "id": "apv_1", "title": payload,
                       "what": payload, "why_waiting": payload, "task_ref": payload}],
            "upstream_errors": [], "counts": {},
        })
        html = client.get("/").text
        assert payload not in html, "未转义的用户数据被拼进 HTML（存储型 XSS）"
        assert "&lt;img src=x onerror=alert(1)&gt;" in html


class TestServiceToken:
    def test_decide_requires_token_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        r = client.post("/console/decide", json={"approval_id": "apv_1", "decision": "approve",
                                                 "comment": "x", "decided_by": "驾驶舱"})
        assert r.status_code == 401

    def test_health_exempt(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.get("/health").status_code == 200
