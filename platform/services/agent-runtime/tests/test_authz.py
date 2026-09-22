"""鉴权专项回归（agent-runtime）：自述决策、自述角色、自述豁免都不得当真。

三条实测问题：
  ① `POST /runs/{tid}/resume` 接受任何调用方自述的 `decision=approve` —— 人审闸门在 HTTP 层可绕过；
  ② `POST /tools/invoke` 的 `role` 与 `allow_ungated` 都来自请求体 —— 调用方可自称任意岗位、
     甚至自称"免人审"；
  ③ `APPROVAL_GATEWAY_URL` 默认端口 8000 与审批网关实际端口（8001，见 run_local_stack.py）不一致。
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.apis import MockApprovalAPI, MockGitLabAPI
from app.llm import MockLLM
from app.main import _config, create_app


class RecordingApprovalAPI(MockApprovalAPI):
    """可按用例定制"审批网关里到底记了什么决策"的假网关。"""

    def __init__(self, record: dict | None = None):
        super().__init__()
        self.record = record or {}

    def get_approval(self, approval_id: str) -> dict:
        if approval_id not in self.record:
            raise RuntimeError("404 Not Found")
        return self.record[approval_id]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DB_URL", f"sqlite:///{tmp_path}/runs.db")
    from app import runs
    runs.reset_engine(f"sqlite:///{tmp_path}/runs.db")
    monkeypatch.setenv("GATE_DECISION_VERIFY", "block")
    llm = MockLLM(answers=["澄清", "PRD v1", "PRD v2 修订版内容", "诊断", "复盘"])
    app = create_app(approval_api=RecordingApprovalAPI(), gitlab_api=MockGitLabAPI(), llm=llm)
    with TestClient(app) as client:
        yield {"client": client, "app": app}
    runs._engine = None


def _start(client, task_ref="authz-1"):
    r = client.post("/runs", json={"requirement_text": "probe requirement", "task_ref": task_ref})
    assert r.status_code == 200, r.text
    return r.json()


class TestResumeRequiresRealDecision:
    def test_missing_approval_id_is_refused_in_block_mode(self, env):
        run = _start(env["client"], "authz-resume")
        tid = run["thread_id"]
        assert env["client"].get(f"/runs/{tid}/state").json()["next"] == ["gate_g1"]
        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "我说了算"})
        assert r.status_code == 409 and "approval_id" in r.json()["detail"]
        # 闸门必须还在原地：拒绝要拒绝干净
        assert env["client"].get(f"/runs/{tid}/state").json()["next"] == ["gate_g1"]

    def test_unknown_approval_id_is_refused(self, env):
        run = _start(env["client"], "authz-resume2")
        r = env["client"].post(f"/runs/{run['thread_id']}/resume",
                               json={"decision": "approve", "approval_id": "apv_missing", "comment": "x"})
        assert r.status_code == 409 and "拿不到证据" in r.json()["detail"]

    def test_status_mismatch_is_refused(self, env):
        """审批单其实被否决了，却拿它来 resume approve —— 拒绝。"""
        env["app"].state.ctx.approval_api.record["apv_rej"] = {
            "id": "apv_rej", "status": "rejected", "type": "GATE_REQUIREMENT_REVIEW", "task_ref": "authz-mismatch",
        }
        run = _start(env["client"], "authz-mismatch")
        r = env["client"].post(f"/runs/{run['thread_id']}/resume",
                               json={"decision": "approve", "approval_id": "apv_rej", "comment": "x"})
        assert r.status_code == 409 and "不一致" in r.json()["detail"]

    def test_task_ref_mismatch_is_refused(self, env):
        env["app"].state.ctx.approval_api.record["apv_other"] = {
            "id": "apv_other", "status": "approved", "type": "GATE_REQUIREMENT_REVIEW", "task_ref": "别的需求",
        }
        run = _start(env["client"], "authz-other")
        r = env["client"].post(f"/runs/{run['thread_id']}/resume",
                               json={"decision": "approve", "approval_id": "apv_other", "comment": "x"})
        assert r.status_code == 409 and "同一件事" in r.json()["detail"]

    def test_gate_type_mismatch_is_refused(self, env):
        env["app"].state.ctx.approval_api.record["apv_wrong_gate"] = {
            "id": "apv_wrong_gate", "status": "approved", "type": "GATE_PR_REVIEW", "task_ref": "authz-gate",
        }
        run = _start(env["client"], "authz-gate")
        r = env["client"].post(f"/runs/{run['thread_id']}/resume",
                               json={"decision": "approve", "approval_id": "apv_wrong_gate", "comment": "x"})
        assert r.status_code == 409 and "不匹配" in r.json()["detail"]

    def test_matching_decision_resumes_and_is_archived(self, env):
        env["app"].state.ctx.approval_api.record["apv_ok"] = {
            "id": "apv_ok", "status": "approved", "type": "GATE_REQUIREMENT_REVIEW", "task_ref": "authz-ok",
        }
        run = _start(env["client"], "authz-ok")
        tid = run["thread_id"]
        r = env["client"].post(f"/runs/{tid}/resume",
                               json={"decision": "approve", "approval_id": "apv_ok", "comment": "同意"})
        assert r.status_code == 200, r.text
        archive = env["client"].get(f"/runs/{tid}/archive").json()
        assert archive["detail"]["gate_decision_evidence"] == "verified"
        assert archive["detail"]["approval_id"] == "apv_ok"

    def test_warn_mode_allows_without_id_but_logs(self, env, monkeypatch):
        monkeypatch.setenv("GATE_DECISION_VERIFY", "warn")
        run = _start(env["client"], "authz-warn")
        r = env["client"].post(f"/runs/{run['thread_id']}/resume",
                               json={"decision": "revise", "comment": "本地调试"})
        assert r.status_code == 200, r.text

    def test_default_mode_follows_service_auth(self, monkeypatch):
        from app import gate_decisions

        monkeypatch.delenv("GATE_DECISION_VERIFY", raising=False)
        monkeypatch.setenv("SERVICE_AUTH_MODE", "off")
        assert gate_decisions.mode() == "warn"
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        assert gate_decisions.mode() == "block", "受管控部署下必须默认强校验"


class TestToolsInvokeTrust:
    def test_allow_ungated_from_body_is_refused_by_default(self, env, monkeypatch):
        monkeypatch.delenv("TOOLS_ALLOW_UNGATED", raising=False)
        r = env["client"].post("/tools/invoke", json={
            "tool": "ops.execute", "params": {"action": "restart", "target": "reports-exporter"},
            "role": "ops-duty", "allow_ungated": True,
        })
        assert r.status_code == 403 and r.json()["detail"]["reason"] == "ungated_not_allowed"

    def test_allow_ungated_honoured_when_explicitly_enabled(self, env, monkeypatch):
        monkeypatch.setenv("TOOLS_ALLOW_UNGATED", "1")
        r = env["client"].post("/tools/invoke", json={
            "tool": "ops.execute", "params": {"action": "restart", "target": "reports-exporter"},
            "role": "ops-duty", "allow_ungated": True,
        })
        assert r.status_code != 403 or r.json()["detail"].get("reason") != "ungated_not_allowed"

    def test_role_from_body_not_trusted_under_token_mode(self, env, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        monkeypatch.delenv("TOOLS_ALLOW_CLIENT_ROLE", raising=False)
        r = env["client"].post("/tools/invoke", json={"tool": "kb.search", "params": {"query": "x"},
                                                     "role": "dev-agent"},
                               headers={"X-Service-Token": "svc"})
        assert r.status_code == 403 and r.json()["detail"]["reason"] == "role_not_trusted"

    def test_unauthenticated_write_refused_under_token_mode(self, env, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert env["client"].post("/runs", json={"requirement_text": "x"}).status_code == 401
        assert env["client"].get("/health").status_code == 200


class TestConfigDefaults:
    def test_approval_gateway_default_port_matches_local_stack(self):
        """默认端口必须与 run_local_stack.py 的端口表一致（此前是 8000，实际网关在 8001）。"""
        from app import apis, main

        src = inspect.getsource(main) + inspect.getsource(apis)
        assert "localhost:8000" not in src
        assert "127.0.0.1:8001" in src

    def test_gateway_url_env_still_wins(self, monkeypatch):
        """环境变量仍是权威：显式配置 GAETWAY_URL 时按配置走（默认值只在未配置时生效）。"""
        monkeypatch.setenv("APPROVAL_GATEWAY_URL", "http://gw.internal:9000")
        import http.server
        import threading

        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"apv_env","status":"approved","type":"GATE_REQUIREMENT_REVIEW"}')

            def log_message(self, *a):  # noqa: D102
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("APPROVAL_GATEWAY_URL", f"http://127.0.0.1:{srv.server_port}")
        try:
            from app.apis import ApprovalAPI
            from app.main import _config as _cfg  # noqa: F401  （确认模块可导入）
            assert ApprovalAPI(os.environ["APPROVAL_GATEWAY_URL"]).get_approval("apv_env")["status"] == "approved"
            assert seen["path"] == "/approvals/apv_env"
        finally:
            srv.shutdown()
