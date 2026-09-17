"""WP4 规则包被 Agent 实际引用测试（验收③：prompt 指纹可查，不是"写了没人读"）。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import rules_pack
from app.coder import Coder

ROOT = Path(__file__).resolve().parents[3]
REAL_PACK = ROOT / "project-rules"


class CapturingLLM:
    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, messages):
        self.prompts.append("\n".join(m.get("content", "") for m in messages))
        return '{"files": []}'


def _coder(tmp_path, monkeypatch, pack_dir: Path):
    monkeypatch.setenv("PROJECT_RULES_DIR", str(pack_dir))
    llm = CapturingLLM()
    return Coder(llm=llm, gitlab_url="http://gitlab", gitlab_token="t", project_path="p/p",
                 push=False, runner=None, gate_api=None), llm


class TestPackLoading:
    def test_real_pack_loads_with_fingerprint(self):
        pack = rules_pack.load_pack(REAL_PACK)
        assert pack["available"] is True
        assert pack["fingerprint"] and len(pack["fingerprint"]) == 16
        counts = {k: len(v) for k, v in pack["layers"].items()}
        assert counts == {"constraints": 3, "design": 2, "completeness": 2}

    def test_missing_pack_is_not_fatal(self, tmp_path):
        pack = rules_pack.load_pack(tmp_path)
        assert pack["available"] is False and pack["fingerprint"] == ""
        assert rules_pack.prompt_excerpt(pack) == ""

    def test_fingerprint_changes_with_rule_content(self, tmp_path):
        dst = tmp_path / "pack"
        shutil.copytree(REAL_PACK, dst)
        before = rules_pack.load_pack(dst)["fingerprint"]
        target = dst / "constraints" / "api-protocol.md"
        target.write_text(target.read_text(encoding="utf-8") + "\n## 新增小节\n必须使用 UTC+8。\n", encoding="utf-8")
        after = rules_pack.load_pack(dst)["fingerprint"]
        assert before != after, "规则内容变了指纹必须变（否则无法核查用的是哪版）"

    def test_excerpt_contains_fingerprint_and_key_rules(self):
        text = rules_pack.prompt_excerpt(rules_pack.load_pack(REAL_PACK))
        assert "指纹" in text and "code" in text and "export:user" in text
        assert "api-protocol.md" in text and "digest" in text

    def test_stats_shape(self):
        info = rules_pack.stats(rules_pack.load_pack(REAL_PACK))
        assert info["available"] and info["counts"]["constraints"] == 3
        assert "constraints/api-protocol.md" in info["files"]["constraints"]


class TestPromptInjection:
    def test_coder_prompt_contains_rules_pack(self, tmp_path, monkeypatch):
        coder, llm = _coder(tmp_path, monkeypatch, REAL_PACK)
        prompt = coder._build_prompt({"title": "导出", "task_ref": "42#1"}, "PRD v1", "方案", [], "")
        assert "项目规则包" in prompt, "规则包必须真的进 prompt"
        assert "api-protocol.md" in prompt
        assert rules_pack.load_pack(REAL_PACK)["fingerprint"] in prompt

    def test_prompt_changes_when_rules_change(self, tmp_path, monkeypatch):
        dst = tmp_path / "pack"
        shutil.copytree(REAL_PACK, dst)
        coder, _ = _coder(tmp_path, monkeypatch, dst)
        before = coder._build_prompt({"title": "t"}, "PRD", "方案", [], "")
        target = dst / "constraints" / "boundaries.md"
        target.write_text(target.read_text(encoding="utf-8") + "\n## 补充红线\n禁止绕过 E2 闸门。\n", encoding="utf-8")
        after = coder._build_prompt({"title": "t"}, "PRD", "方案", [], "")
        assert before != after and "禁止绕过 E2 闸门" in after

    def test_missing_pack_does_not_break_prompt(self, tmp_path, monkeypatch):
        coder, _ = _coder(tmp_path, monkeypatch, tmp_path / "nope")
        prompt = coder._build_prompt({"title": "t"}, "PRD", "方案", [], "")
        assert "# PRD（验收标准）" in prompt      # 仍能构造 prompt
        assert "项目规则包" not in prompt

    def test_endpoint_exposes_pack(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import create_app

        monkeypatch.setenv("PROJECT_RULES_DIR", str(REAL_PACK))
        client = TestClient(create_app())
        info = client.get("/rules-pack").json()
        assert info["available"] is True and info["fingerprint"]
        assert info["counts"]["completeness"] == 2
