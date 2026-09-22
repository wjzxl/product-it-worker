"""WP10 驾驶舱测试：待决策队列聚合、深链、决策提交与误拦反馈（不绕过审计）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import console
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


APPROVALS = [{
    "id": "apv_1", "type": "GATE_REQUIREMENT_REVIEW", "stage": "产品域 G1", "task_ref": "tsk_1",
    "created_at": "2026-09-17T10:00:00", "trust_level": "L2",
    "context_snapshot": {"diff_summary": "起草 PRD v1", "evidence": ["https://prd"],
                         "risk": {"level": "L2"}, "sources": [{"kind": "voice", "ref": "v1"}]},
}]
GATE_CHECKS = [
    {"id": "gck_1", "code": "E1", "task_ref": "tsk_2", "blocked": True, "passed": False,
     "gap_ids": ["impact_scope"], "checked_at": "2026-09-17T11:00:00", "feedback": ""},
    {"id": "gck_2", "code": "E2", "task_ref": "tsk_3", "blocked": False, "passed": True,
     "gap_ids": [], "checked_at": "2026-09-17T11:05:00", "feedback": ""},
]
RUNS = {"runs": [{"thread_id": "sched-1", "role": "ops-duty", "sop": "ops-domain/alert-to-postmortem",
                  "status": "waiting", "waiting_reason": "指标/日志尚未到齐", "waits": 1,
                  "task_ref": "t-9", "started_at": "2026-09-17T09:00:00"}],
        "stats": {}}


@pytest.fixture()
def fake_upstreams(monkeypatch):
    def fake_get(url, params=None, timeout=8.0):
        if url.endswith("/approvals"):
            return APPROVALS
        if url.endswith("/eng-gates/checks"):
            return GATE_CHECKS
        if url.endswith("/runs"):
            return RUNS
        if url.endswith("/eng-gates/feedback/stats"):
            return {"blocked_total": 3, "judged": 1, "false_positive": 0, "false_positive_rate": 0.0}
        return None
    monkeypatch.setattr(console, "_get", fake_get)
    yield


class TestQueue:
    def test_queue_aggregates_three_sources(self, fake_upstreams):
        data = console.build_queue()
        assert data["counts"] == {"approvals": 1, "gate_blocks": 1, "run_waits": 1, "total": 3}
        kinds = {item["kind"] for item in data["queue"]}
        assert kinds == {"approval", "gate_block", "run_waiting"}

    def test_items_carry_decision_context_and_links(self, fake_upstreams):
        data = console.build_queue()
        apv = next(i for i in data["queue"] if i["kind"] == "approval")
        assert apv["what"] and apv["why_waiting"] and apv["deep_link"].startswith("/approvals/")
        assert apv["source_count"] == 1 and apv["actions"] == ["approve", "reject", "revise"]
        gate = next(i for i in data["queue"] if i["kind"] == "gate_block")
        assert "impact_scope" in gate["what"] and gate["actions"] == ["feedback_valid", "feedback_false_positive"]
        run = next(i for i in data["queue"] if i["kind"] == "run_waiting")
        assert "wake" in run["actions"] and "指标" in run["why_waiting"]

    def test_only_blocked_checks_enter_queue(self, fake_upstreams):
        data = console.build_queue()
        ids = [i["id"] for i in data["queue"]]
        assert "gck_1" in ids and "gck_2" not in ids      # 通过的检查不该打扰人

    def test_upstream_failure_is_reported_not_faked(self, monkeypatch):
        monkeypatch.setattr(console, "_get", lambda url, params=None, timeout=8.0: None)
        data = console.build_queue()
        assert data["queue"] == [] and len(data["upstream_errors"]) == 3

    def test_endpoint_returns_queue(self, client, fake_upstreams):
        body = client.get("/console").json()
        assert body["counts"]["total"] == 3 and body["feedback_stats"]["blocked_total"] == 3


class TestActions:
    def test_decide_proxies_to_approval_gateway(self, client, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=20):
            captured.update({"url": url, "json": json})
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"id": "dec_1", "decision": json["decision"]}
            return R()

        monkeypatch.setattr(console.httpx, "post", fake_post)
        r = client.post("/console/decide", json={"approval_id": "apv_1", "decision": "approve",
                                                 "comment": "OK", "decided_by": "值班经理"})
        assert r.status_code == 200 and r.json()["decision"] == "approve"
        assert captured["url"].endswith("/approvals/apv_1/decision")
        assert captured["json"]["decided_by"] == "值班经理"      # 决策人写入审计

    def test_invalid_decision_rejected(self, client):
        assert client.post("/console/decide", json={"approval_id": "x", "decision": "随便"}).status_code == 422

    def test_feedback_proxies_and_validates(self, client, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=20):
            captured.update({"url": url, "json": json})
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"feedback": json["verdict"]}   # check_id 在 URL 里
            return R()

        monkeypatch.setattr(console.httpx, "post", fake_post)
        r = client.post("/console/feedback", json={"check_id": "gck_1", "verdict": "false_positive",
                                                   "note": "影响范围其实在 PRD 里写了"})
        assert r.status_code == 200 and r.json()["feedback"] == "false_positive"
        assert captured["url"].endswith("/eng-gates/checks/gck_1/feedback")
        assert client.post("/console/feedback", json={"check_id": "x", "verdict": "乱写"}).status_code == 422

    def test_upstream_error_becomes_502(self, client, monkeypatch):
        def boom(url, json=None, headers=None, timeout=20):
            raise RuntimeError("网关挂了")
        monkeypatch.setattr(console.httpx, "post", boom)
        r = client.post("/console/decide", json={"approval_id": "apv_1", "decision": "approve"})
        assert r.status_code == 502 and "审批网关决策失败" in r.json()["detail"]


class TestDashboardHtml:
    def test_panel_shows_queue_and_feedback_rate(self, client, fake_upstreams):
        html = client.get("/").text
        assert "待决策队列" in html and "误拦率" in html
        assert "approve" in html and "false_positive" in html      # 动作按钮已渲染
