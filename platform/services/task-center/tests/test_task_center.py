"""任务中心测试：状态机、事件接入联动、幂等收件箱、时间线。"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='taskcenter-test-')}/test.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _issue_event(event_id: str, iid: int = 12) -> dict:
    return {
        "event_id": event_id,
        "event_type": "gitlab.issue.opened",
        "source": "gitlab-webhook",
        "trace_id": f"gl-{event_id}",
        "payload": {
            "project_id": 42,
            "path_with_namespace": "sandbox/prodesgin",
            "iid": iid,
            "title": "报表导出功能",
            "description": "支持按日期筛选",
            "labels": ["product"],
        },
    }


class TestStateMachine:
    def test_create_and_valid_flow(self, client):
        r = client.post("/tasks", json={"external_ref": "manual-1", "sop": "product-domain/prd-flow"})
        assert r.status_code == 201
        task = r.json()
        assert task["status"] == "pending"

        r = client.post(f"/tasks/{task['id']}/transition", json={"to": "running", "by": "产品Agent", "detail": {"step": "draft_prd"}})
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        assert r.json()["current_step"] == "draft_prd"

    def test_invalid_transition_rejected(self, client):
        r = client.post("/tasks", json={"external_ref": "manual-2"})
        task = r.json()
        r = client.post(f"/tasks/{task['id']}/transition", json={"to": "done"})
        assert r.status_code == 409  # pending 不能直接 done


class TestEventIngestion:
    def test_issue_event_creates_task(self, client):
        r = client.post("/events", json=_issue_event("e-issue-1"))
        assert r.status_code == 200
        assert r.json()["status"] == "created"
        task = client.get(f"/tasks/{r.json()['task_id']}").json()
        assert task["external_ref"] == "42#12"
        assert task["sop"] == "product-domain/prd-flow"
        assert task["params"]["title"] == "报表导出功能"

    def test_duplicate_event_id_skipped(self, client):
        r1 = client.post("/events", json=_issue_event("e-issue-2", iid=13))
        assert r1.json()["status"] == "created"
        r2 = client.post("/events", json=_issue_event("e-issue-2", iid=13))
        assert r2.json()["status"] == "duplicate_skipped"
        tasks = client.get("/tasks").json()
        assert sum(1 for t in tasks if t["external_ref"] == "42#13") == 1

    def test_approval_flow_advances_task(self, client):
        r = client.post("/events", json=_issue_event("e-issue-3", iid=14))
        task_id = r.json()["task_id"]
        # Agent 领取
        client.post(f"/tasks/{task_id}/transition", json={"to": "running", "by": "产品Agent", "detail": {"step": "draft_prd"}})

        # 到达 Gate → waiting_approval
        r = client.post(
            "/events",
            json={
                "event_id": "e-apv-req-1", "event_type": "approval.requested", "source": "approval-gateway",
                "payload": {"task_ref": "42#14", "approval_id": "apv_x", "gate": "GATE_REQUIREMENT_REVIEW"},
            },
        )
        assert r.json()["status"] == "ok"
        assert client.get(f"/tasks/{task_id}").json()["status"] == "waiting_approval"

        # 批准 → 恢复 running
        client.post(
            "/events",
            json={
                "event_id": "e-apv-dec-1", "event_type": "approval.decided", "source": "approval-gateway",
                "payload": {"task_ref": "42#14", "decision": "approve", "decided_by": "产品负责人@Alice", "comment": ""},
            },
        )
        task = client.get(f"/tasks/{task_id}").json()
        assert task["status"] == "running"
        types = [e["event_type"] for e in task["events"]]
        assert types == ["task.created", "status_changed", "approval.requested", "approval.decided"]

    def test_reject_flow(self, client):
        r = client.post("/events", json=_issue_event("e-issue-4", iid=15))
        task_id = r.json()["task_id"]
        client.post(f"/tasks/{task_id}/transition", json={"to": "running", "by": "产品Agent"})
        client.post(
            "/events",
            json={
                "event_id": "e-apv-req-2", "event_type": "approval.requested", "source": "approval-gateway",
                "payload": {"task_ref": "42#15"},
            },
        )
        client.post(
            "/events",
            json={
                "event_id": "e-apv-dec-2", "event_type": "approval.decided", "source": "approval-gateway",
                "payload": {"task_ref": "42#15", "decision": "reject", "decided_by": "产品负责人@Alice", "comment": "范围超出本迭代"},
            },
        )
        task = client.get(f"/tasks/{task_id}").json()
        assert task["status"] == "rejected"
        # 终态后不允许再流转
        r = client.post(f"/tasks/{task_id}/transition", json={"to": "running"})
        assert r.status_code == 409

    def test_unknown_event_ignored(self, client):
        r = client.post(
            "/events",
            json={"event_id": "e-unknown-1", "event_type": "gitlab.mr.opened", "source": "gitlab-webhook", "payload": {"iid": 45}},
        )
        assert r.json()["status"] == "ignored"

    def test_auto_dev_label_triggers_runtime(self, client, monkeypatch):
        """Issue 带 auto-dev 标签 → 自动调用 agent-runtime /runs（研发域 SOP）。"""
        import httpx

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            req = httpx.Request("POST", url, json=json)
            return httpx.Response(200, json={"status": "waiting_approval"}, request=req)

        monkeypatch.setenv("AGENT_RUNTIME_URL", "http://localhost:8003")
        monkeypatch.setattr("httpx.post", fake_post)
        r = client.post(
            "/events",
            json={
                "event_id": "e-auto-1",
                "event_type": "gitlab.issue.opened",
                "source": "gitlab-webhook",
                "payload": {
                    "project_id": 85697055,
                    "iid": 88,
                    "title": "按类别汇总报表",
                    "description": "## 验收标准\n- WHEN 传入数据 THEN 按类别聚合",
                    "labels": ["auto-dev"],
                },
            },
        )
        assert r.status_code == 200 and r.json()["status"] == "created"
        assert captured["url"].endswith("/runs")
        body = captured["json"]
        assert body["sop"] == "dev-domain/issue-to-merge"
        assert body["task_ref"] == "85697055#88"
        assert body["project_id"] == "85697055"


class TestPrometheusAlerts:
    def test_firing_alert_creates_task_and_triggers_ops(self, client, monkeypatch):
        """Prometheus firing 告警 → 建任务 + 自动触发运维域 SOP（含告警载荷）。"""
        import httpx

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(200, json={"status": "waiting_approval"}, request=httpx.Request("POST", url, json=json))

        monkeypatch.setenv("AGENT_RUNTIME_URL", "http://localhost:8003")
        monkeypatch.setenv("OPS_PROJECT_ID", "85697055")
        monkeypatch.setattr("httpx.post", fake_post)

        r = client.post(
            "/alerts/prometheus",
            json={
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {"alertname": "ExportHighLatency", "severity": "P1", "job": "reports-exporter", "fingerprint": "abc123"},
                        "annotations": {"summary": "导出接口 p95 12s", "description": "p95 12s 超过 SLO 3s"},
                    }
                ],
            },
        )
        assert r.status_code == 200 and len(r.json()["tasks"]) == 1
        assert captured["url"].endswith("/runs")
        body = captured["json"]
        assert body["sop"] == "ops-domain/alert-to-postmortem"
        assert body["alert"]["title"] == "ExportHighLatency"
        assert body["alert"]["severity"] == "P1"
        assert "SLO 3s" in body["context"]
        assert body["project_id"] == "85697055"
        # 任务已建（sop=运维域）
        tasks = client.get("/tasks").json()
        assert any(t["sop"] == "ops-domain/alert-to-postmortem" for t in tasks)

    def test_resolved_alert_ignored(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_RUNTIME_URL", "http://localhost:8003")
        r = client.post(
            "/alerts/prometheus",
            json={"status": "resolved", "alerts": [{"status": "resolved", "labels": {"alertname": "X"}}]},
        )
        assert r.json()["tasks"] == []


class TestTimeline:
    def test_append_event(self, client):
        task = client.post("/tasks", json={"external_ref": "manual-3"}).json()
        r = client.post(f"/tasks/{task['id']}/events", json={"event_type": "step_finished", "by": "产品Agent", "detail": {"step": "draft_prd"}})
        assert r.status_code == 200
        assert r.json()["events"][-1]["event_type"] == "step_finished"
