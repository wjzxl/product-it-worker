"""WP2 工程闸门在 agent-runtime 侧的接入测试。

核心语义（计划 WP2 验收 ①）：**E1 未通过时必须在调用 LLM 之前就停止**，
否则"闸门"退化成事后提示。这里用计数型假 LLM 证明它一次都没被调用。
"""
from __future__ import annotations

import pytest

from app.apis import GateAPI
from app.coder import CodingError, Coder
from app.gate_text import render_gaps


class CountingLLM:
    """记录调用次数的假 LLM：E1 拦截时必须是 0 次。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return '{"files": [{"path": "src/x.py", "content": "print(1)"}]}'


class FakeGate:
    def __init__(self, blocked: bool, gaps=None):
        self.blocked = blocked
        self.calls: list[dict] = []
        self._gaps = gaps or [{"id": "impact_scope", "ask": "会影响哪些已有接口/表/任务",
                               "severity": "critical", "detail": "未提供", "hint": "答不上来就停下"}]

    def check(self, code, task_ref="", context=None, mode=None, checked_by=""):
        self.calls.append({"code": code, "task_ref": task_ref, "context": context, "mode": mode, "checked_by": checked_by})
        return {"code": code, "mode": mode or "block", "passed": not self.blocked, "blocked": self.blocked,
                "gaps": self._gaps if self.blocked else [], "results": [], "critical_missing":
                ["impact_scope"] if self.blocked else [], "rule_version": "1", "fingerprint": "abc", "check_id": "gck_x"}


def _coder(gate, llm):
    return Coder(llm=llm, gitlab_url="http://gitlab", gitlab_token="t", project_path="p/p",
                 push=False, runner=None, gate_api=gate)


class TestGateBlocksBeforeLLM:
    def test_blocked_raises_and_llm_not_called(self):
        llm, gate = CountingLLM(), FakeGate(blocked=True)
        coder = _coder(gate, llm)
        with pytest.raises(CodingError) as exc:
            coder.implement(task={"task_ref": "42#7", "title": "x"}, prd="PRD v1", tech_design="design")
        assert "E1 工程闸门未通过" in str(exc.value)
        assert "关键未知不能被多数通过抵消" in str(exc.value)
        assert llm.calls == 0, "闸门拦截时不得调用 LLM"

    def test_gate_context_includes_inferable_fields(self):
        gate = FakeGate(blocked=True)
        coder = _coder(gate, CountingLLM())
        with pytest.raises(CodingError):
            coder.implement(
                task={"task_ref": "42#8", "impact_scope": "复用 /api/v1/report/export", "rollback_plan": "切回上一版本",
                      "sources": [{"kind": "voice", "ref": "v1"}]},
                prd="PRD v2", tech_design="d", only_src=True, allowed_files=["src/a.py"],
            )
        ctx = gate.calls[0]["context"]
        assert ctx["prd_ref"].startswith("PRD v2")
        assert ctx["sources"] and ctx["impact_scope"] and ctx["rollback_plan"]
        assert ctx["change_targets"] == ["src/a.py"]
        assert gate.calls[0]["checked_by"] == "agent:coder"

    def test_passed_gate_lets_flow_continue(self, tmp_path):
        """闸门通过时正常进入实现流程（假 LLM 至少被调用一次）。

        用临时 git 仓库作为 checkout 替身，避免测试去 clone 真实远端（会等网络超时）。
        """
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "commit",
                        "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)

        llm = CountingLLM()
        coder = _coder(FakeGate(blocked=False), llm)
        try:
            coder.implement(task={"task_ref": "42#9", "impact_scope": "无", "rollback_plan": "回滚"},
                            prd="PRD", tech_design="d", workdir=str(repo))
        except Exception:
            pass  # 后续（测试执行/提交）需要更多环境，此处只验闸门放行后确实进入了实现
        assert llm.calls >= 1, "闸门通过后应继续实现"

    def test_no_gate_client_is_noop(self):
        """未注入闸门客户端时不阻断（离线/单测场景），但也不假装检查过。"""
        llm = CountingLLM()
        coder = _coder(None, llm)
        assert coder._pre_implementation_gate({"task_ref": "x"}, "PRD", "d") == {
            "blocked": False, "passed": True, "mode": "off", "gaps": [], "_text": ""
        }


class TestGateApiClient:
    def test_unavailable_gate_defaults_to_not_blocking(self, monkeypatch):
        monkeypatch.delenv("ENG_GATES_REQUIRED", raising=False)
        client = GateAPI("http://127.0.0.1:1", required=False)
        result = client.check("E1", task_ref="t")
        assert result["blocked"] is False and result["passed"] is True
        assert "unavailable" in result

    def test_unavailable_gate_blocks_when_required(self):
        client = GateAPI("http://127.0.0.1:1", required=True)
        result = client.check("E1", task_ref="t")
        assert result["blocked"] is True and result["passed"] is False
        assert result["gaps"][0]["id"] == "gate_unavailable"


class TestRenderGaps:
    def test_render_marks_critical(self):
        text = render_gaps({"code": "E1", "blocked": True, "mode": "block",
                            "gaps": [{"ask": "影响范围", "detail": "未提供", "severity": "critical", "hint": "先确认"}],
                            "critical_missing": ["impact_scope"]})
        assert "⛔" in text and "先确认" in text and "关键未知" in text

    def test_render_passed(self):
        assert "全部检查项通过" in render_gaps({"code": "E1", "passed": True, "gaps": []})
