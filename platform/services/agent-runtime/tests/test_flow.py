"""Agent 运行时测试：G1 挂起 / 批准 / 退回修订 / 拒绝 / 状态查询。"""
import pytest
from fastapi.testclient import TestClient

from app.apis import MockApprovalAPI, MockGitLabAPI
from app.llm import MockLLM
from app.main import create_app


@pytest.fixture()
def env():
    approval = MockApprovalAPI()
    gitlab = MockGitLabAPI()
    llm = MockLLM(answers=["PRD v1 内容", "PRD v2 修订版内容"])
    app = create_app(approval_api=approval, gitlab_api=gitlab, llm=llm)
    return {"client": TestClient(app), "approval": approval, "gitlab": gitlab, "llm": llm}


def _start(env, task_ref="42#12"):
    return env["client"].post(
        "/runs",
        json={
            "requirement_text": "用户想要一个可以导出报表的功能，支持按日期筛选",
            "title": "报表导出功能",
            "task_ref": task_ref,
            "project_id": "sandbox/prodesgin",
        },
    )


class TestGateFlow:
    def test_run_suspends_at_g1(self, env):
        r = _start(env)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "waiting_approval"
        assert body["prd_version"] == 1
        assert "PRD v1" in body["prd_doc"]
        assert body["approval_id"].startswith("apv_mock")

        # 审批网关侧收到三要素齐备的审批请求
        payload = env["approval"].requests[-1]
        assert payload["type"] == "GATE_REQUIREMENT_REVIEW"
        assert payload["task_ref"] == "42#12"
        ctx = payload["context_snapshot"]
        assert ctx["diff_summary"] and ctx["evidence"] and ctx["risk"]["level"] == "L2"

    def test_approve_resume_creates_issue(self, env):
        r = _start(env)
        tid = r.json()["thread_id"]

        r2 = env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "OK"})
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "done"
        assert body["issue"]["iid"] >= 12
        assert env["gitlab"].created[-1]["labels"] == ["product", "G1-passed"]
        assert "PRD v1" in env["gitlab"].created[-1]["description"]

    def test_revise_loops_back_and_reapproves(self, env):
        r = _start(env)
        tid = r.json()["thread_id"]
        first_approval = r.json()["approval_id"]

        r2 = env["client"].post(
            f"/runs/{tid}/resume",
            json={"decision": "revise", "comment": "缺少导出权限控制说明"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "waiting_approval"
        assert body["prd_version"] == 2
        assert "PRD v2" in body["prd_doc"]
        assert body["approval_id"] != first_approval  # 修订后重新发起审批

        r3 = env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "已补充"})
        assert r3.json()["status"] == "done"

    def test_reject_ends_flow(self, env):
        r = _start(env, task_ref="42#13")
        tid = r.json()["thread_id"]
        r2 = env["client"].post(
            f"/runs/{tid}/resume",
            json={"decision": "reject", "comment": "范围超出本迭代"},
        )
        body = r2.json()
        assert body["status"] == "rejected"
        assert env["gitlab"].created == []  # 否决后不建 Issue

    def test_invalid_decision_rejected(self, env):
        r = _start(env)
        tid = r.json()["thread_id"]
        r2 = env["client"].post(f"/runs/{tid}/resume", json={"decision": "maybe"})
        assert r2.status_code == 422

    def test_state_endpoint(self, env):
        r = _start(env)
        tid = r.json()["thread_id"]
        state = env["client"].get(f"/runs/{tid}/state").json()
        assert "gate_g1" in state["next"]
        assert state["interrupts"][0]["gate"] == "G1_REQUIREMENT_REVIEW"
        assert state["values"]["prd_doc"].startswith("PRD v1")
