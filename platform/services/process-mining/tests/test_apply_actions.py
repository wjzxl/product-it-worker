"""扩展应用动作测试：Prompt 版本化 + 知识库自动入库（含校验）。"""
import json

import httpx
import pytest
import yaml

from app.apply import apply_action
from app.proposals import generate


@pytest.fixture()
def prompt_env(tmp_path, monkeypatch):
    (tmp_path / "registry.yaml").write_text(
        yaml.safe_dump({"version": 1, "prompts": {"coder_agent": "v1"}}, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "coder_agent.v1.md").write_text("v1 内容", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path))
    return tmp_path


class TestPromptVersion:
    def test_apply_creates_new_version_and_switches_active(self, prompt_env):
        new_prompt = "你是编码 Agent。" + "要求：最小化改动，禁止重写整个文件。" * 5
        res = apply_action({"type": "prompt_version", "target": "coder_agent", "value": new_prompt})
        assert res["applied"] and res["before"] == "v1" and res["after"] == "v2"
        assert (prompt_env / "coder_agent.v2.md").read_text(encoding="utf-8").startswith("你是编码 Agent。")
        reg = yaml.safe_load((prompt_env / "registry.yaml").read_text(encoding="utf-8"))
        assert reg["prompts"]["coder_agent"] == "v2" and reg["version"] == 2

    def test_unknown_key_rejected(self, prompt_env):
        res = apply_action({"type": "prompt_version", "target": "ghost_agent", "value": "x" * 60})
        assert res["applied"] is False and "未知 Prompt 键" in res["reason"]

    def test_too_short_content_rejected(self, prompt_env):
        res = apply_action({"type": "prompt_version", "target": "coder_agent", "value": "太短"})
        assert res["applied"] is False and "50~8000" in res["reason"]


class TestKnowledgeIngest:
    def test_ingest_posts_document(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers or {}
            return httpx.Response(200, json={"source": json["source"], "chunks": 3},
                                  request=httpx.Request("POST", url, json=json))

        monkeypatch.setattr("app.apply.httpx.post", fake_post)
        content = "## 新规范：导出接口日期范围上限 365 天\nWHEN 跨度超过 365 天 THEN 返回空列表并提示用户。"
        res = apply_action({"type": "knowledge_ingest", "target": "runbooks", "value": content})
        assert res["applied"] and res["chunks"] == 3
        assert res["source"].startswith("runbooks/auto-")
        assert captured["url"].endswith("/documents") and captured["json"]["content"] == content

    def test_invalid_source_and_short_content_rejected(self):
        assert apply_action({"type": "knowledge_ingest", "target": "../etc", "value": "x" * 50})["applied"] is False
        assert apply_action({"type": "knowledge_ingest", "target": "runbooks", "value": "短"})["applied"] is False


class TestProposalMarking:
    def test_new_action_types_are_appliable(self):
        report = {"gates": [], "bottlenecks": [], "variants": [], "cost_by_model": [], "top_rework_reasons": []}
        chat = lambda prompt: json.dumps({"proposals": [  # noqa: E731
            {"title": "升级编码 Prompt", "rationale": "r", "evidence": "e",
             "action": {"type": "prompt_version", "target": "coder_agent", "value": "x" * 60}},
            {"title": "补充 Runbook", "rationale": "r", "evidence": "e",
             "action": {"type": "knowledge_ingest", "target": "runbooks", "value": "y" * 60}},
            {"title": "建议", "rationale": "r", "evidence": "e", "action": {"type": "advice", "target": "x", "value": "y"}},
        ]}, ensure_ascii=False)
        props = generate(report, chat=chat)
        assert [p["appliable"] for p in props] == [True, True, False]
