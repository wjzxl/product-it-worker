"""GitLab API 客户端测试（httpx.MockTransport，零外部依赖）。"""
import json

import httpx
import pytest

from app.client import GitLabAPIError, GitLabClient


def test_create_issue_sends_correct_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["json"] = json.loads(request.content)
        captured["token"] = request.headers.get("PRIVATE-TOKEN")
        return httpx.Response(201, json={"iid": 12, "title": "报表导出"})

    client = GitLabClient("https://gitlab.example.com", "tok-123", transport=httpx.MockTransport(handler))
    result = client.create_issue("sandbox/prodesgin", "报表导出", "支持 CSV", ["product", "G1-passed"])

    assert result["iid"] == 12
    assert captured["url"] == "https://gitlab.example.com/api/v4/projects/sandbox%2Fprodesgin/issues"
    assert captured["method"] == "POST"
    assert captured["token"] == "tok-123"
    assert captured["json"] == {
        "title": "报表导出",
        "description": "支持 CSV",
        "labels": "product,G1-passed",
    }


def test_comment_mr():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(201, json={"id": 7})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    client.comment_mr(42, 45, "AI 预审：验收标准逐条核对通过")

    assert captured["url"].endswith("/api/v4/projects/42/merge_requests/45/notes")


def test_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "404 Project Not Found"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(GitLabAPIError, match="404"):
        client.get_issue("no/such", 1)
