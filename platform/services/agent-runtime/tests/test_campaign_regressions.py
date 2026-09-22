"""camp-pst-20260922 缺陷回归：BUG-007（/wake 守卫）、BUG-012（否决归档）。"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.apis import MockApprovalAPI, MockGitLabAPI
from app.llm import MockLLM
from app.main import create_app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DB_URL", f"sqlite:///{tmp_path}/runs.db")
    from app import runs
    runs.reset_engine(f"sqlite:///{tmp_path}/runs.db")
    llm = MockLLM(answers=["澄清", "PRD v1", "PRD v2 修订版内容", "诊断", "复盘"])
    app = create_app(approval_api=MockApprovalAPI(), gitlab_api=MockGitLabAPI(), llm=llm)
    with TestClient(app) as client:
        yield {"client": client, "app": app}
    runs._engine = None


def _start(client, task_ref="reg-1"):
    r = client.post("/runs", json={"requirement_text": "probe requirement", "task_ref": task_ref})
    assert r.status_code == 200, r.text
    return r.json()


class TestBug007WakeGuard:
    def test_wake_on_gate_wait_is_refused(self, env):
        """等待人审的运行不能用 /wake 唤醒（否则会吞掉闸门中断并静默否决）。"""
        run = _start(env["client"], "reg-wake")
        tid = run["thread_id"]
        assert env["client"].get(f"/runs/{tid}/state").json()["next"] == ["gate_g1"]

        r = env["client"].post(f"/runs/{tid}/wake", json={"data": {"note": "probe"}})
        assert r.status_code == 409 and "等待人审" in r.json()["detail"]
        # 闸门中断必须还在，且仍可正常决策
        assert env["client"].get(f"/runs/{tid}/state").json()["next"] == ["gate_g1"]
        resumed = env["client"].post(f"/runs/{tid}/resume",
                                     json={"decision": "revise", "comment": "继续"}).json()
        assert resumed["status"] == "waiting_approval" and resumed["prd_version"] == 2

    def test_wake_on_non_waiting_run_is_refused(self, env):
        run = _start(env["client"], "reg-wake2")
        tid = run["thread_id"]
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "reject", "comment": "不要"})
        r = env["client"].post(f"/runs/{tid}/wake", json={"data": {}})
        assert r.status_code == 409


class TestBug012RejectedArchive:
    def test_rejected_run_is_archived_as_rejected(self, env):
        run = _start(env["client"], "reg-archive")
        tid = run["thread_id"]
        resumed = env["client"].post(f"/runs/{tid}/resume",
                                     json={"decision": "reject", "comment": "not acceptable"}).json()
        assert resumed["status"] == "rejected"
        archive = env["client"].get(f"/runs/{tid}/archive").json()
        assert archive["status"] == "rejected", archive
        stats = env["client"].get("/runs").json()["stats"]
        assert stats["by_status"].get("rejected") == 1
        assert stats["success_rate"] == 0.0        # 0/1 完成，否决必须进入成功率分母

    def test_revise_is_archived_as_waiting_with_marker(self, env):
        run = _start(env["client"], "reg-revise")
        tid = run["thread_id"]
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "revise", "comment": "改"})
        archive = env["client"].get(f"/runs/{tid}/archive").json()
        assert archive["status"] == "waiting" and archive["detail"].get("revised") is True
