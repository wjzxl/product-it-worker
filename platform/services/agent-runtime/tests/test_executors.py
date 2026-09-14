"""操作执行器测试：dryrun 记录 + webhook 真实下发（MockTransport）+ 环境选择。"""
import httpx
import pytest

from app.executors import DryRunExecutor, WebhookExecutor, executor_from_env


def test_dryrun_records_only():
    res = DryRunExecutor().execute("scale_out", "reports-exporter", {"replicas": 2})
    assert res["ok"] is True and res["mode"] == "dryrun"
    assert "scale_out" in res["detail"] and "reports-exporter" in res["detail"]


def test_webhook_executor_posts_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"task_id": "ops-123", "accepted": True})

    ex = WebhookExecutor("https://ops.internal/api/execute", "tok-1", transport=httpx.MockTransport(handler))
    res = ex.execute("restart", "reports-exporter")
    assert res["ok"] is True and res["mode"] == "webhook"
    assert res["response"]["task_id"] == "ops-123"
    assert captured["url"] == "https://ops.internal/api/execute"
    assert captured["auth"] == "Bearer tok-1"
    assert '"action": "restart"' in captured["body"] or '"action":"restart"' in captured["body"]


def test_webhook_executor_handles_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    ex = WebhookExecutor("https://ops.internal/api/execute", "", transport=httpx.MockTransport(handler))
    res = ex.execute("rollback", "reports-exporter")
    assert res["ok"] is False and "失败" in res["detail"]


def test_executor_from_env(monkeypatch):
    monkeypatch.delenv("OPS_EXECUTOR", raising=False)
    assert executor_from_env().mode == "dryrun"

    monkeypatch.setenv("OPS_EXECUTOR", "webhook")
    monkeypatch.setenv("OPS_EXECUTOR_URL", "https://ops.internal/api/execute")
    assert executor_from_env().mode == "webhook"

    monkeypatch.setenv("OPS_EXECUTOR", "webhook")
    monkeypatch.delenv("OPS_EXECUTOR_URL", raising=False)
    assert executor_from_env().mode == "dryrun"  # 缺 URL 回退
