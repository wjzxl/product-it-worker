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


def test_create_mr():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(201, json={"iid": 9, "web_url": "http://x/mr/9"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    result = client.create_mr("sandbox/prodesgin", "feat/report", "main", "feat: 报表导出")
    assert result["iid"] == 9
    assert captured["url"].endswith("/api/v4/projects/sandbox%2Fprodesgin/merge_requests")
    assert captured["json"] == {"source_branch": "feat/report", "target_branch": "main", "title": "feat: 报表导出", "description": ""}


def test_merge_mr():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json={"state": "merged"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    result = client.merge_mr(42, 9)
    assert result["state"] == "merged"
    assert captured["url"].endswith("/api/v4/projects/42/merge_requests/9/merge")
    assert captured["method"] == "PUT"


def test_close_issue():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"state": "closed"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    result = client.close_issue(42, 7)
    assert result["state"] == "closed"
    assert captured["url"].endswith("/api/v4/projects/42/issues/7")
    assert captured["json"] == {"state_event": "close"}


def test_trigger_pipeline():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 99, "status": "pending", "ref": "main"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    result = client.trigger_pipeline(42, "main")
    assert result["id"] == 99 and result["status"] == "pending"
    assert captured["url"].endswith("/api/v4/projects/42/pipeline")
    assert captured["json"] == {"ref": "main"}


def test_get_pipeline_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 1, "name": "test", "status": "failed"}])

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    jobs = client.get_pipeline_jobs(42, 99)
    assert jobs[0]["name"] == "test" and jobs[0]["status"] == "failed"


def test_trigger_pipeline_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": {"base": ["Identity verification is required"]}})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(GitLabAPIError, match="Identity verification"):
        client.trigger_pipeline(42, "main")


def test_create_hook():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 7, "url": "https://platform.example.com/webhooks/gitlab"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    hook = client.create_hook(42, "https://platform.example.com/webhooks/gitlab", "s3cret")
    assert hook["id"] == 7
    assert captured["url"].endswith("/api/v4/projects/42/hooks")
    assert captured["json"]["url"].startswith("https://platform.example.com")
    assert captured["json"]["token"] == "s3cret"
    assert captured["json"]["issues_events"] is True and captured["json"]["pipeline_events"] is True


def test_list_and_delete_hook():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=[{"id": 7, "url": "u"}])

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    assert client.list_hooks(42)[0]["id"] == 7
    assert client.delete_hook(42, 7) == {}


def test_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "404 Project Not Found"})

    client = GitLabClient("https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(GitLabAPIError, match="404"):
        client.get_issue("no/such", 1)
