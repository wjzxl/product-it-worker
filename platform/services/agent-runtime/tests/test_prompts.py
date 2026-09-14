"""Prompt 注册中心测试：加载、热更新（mtime）、缺失回退。"""
import os
import time

import pytest
import yaml

from app import prompts as prompts_mod
from app.prompts import active_versions, get_prompt, registry


@pytest.fixture()
def pdir(tmp_path, monkeypatch):
    (tmp_path / "registry.yaml").write_text(
        yaml.safe_dump({"version": 1, "prompts": {"coder_agent": "v1", "tester_agent": "v1"}}, allow_unicode=True),
        encoding="utf-8",
    )
    (tmp_path / "coder_agent.v1.md").write_text("v1 提示词：输出 JSON。", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path))
    prompts_mod._cache = None
    prompts_mod._cache_mtime = None
    return tmp_path


def test_load_active_version(pdir):
    assert get_prompt("coder_agent", "默认") == "v1 提示词：输出 JSON。"
    assert active_versions()["prompts"]["coder_agent"] == "v1"


def test_fallback_for_unknown_key(pdir):
    assert get_prompt("not_exist", "内置默认") == "内置默认"


def test_fallback_when_file_missing(pdir):
    (pdir / "registry.yaml").write_text(
        yaml.safe_dump({"version": 2, "prompts": {"coder_agent": "v9"}}, allow_unicode=True), encoding="utf-8"
    )
    assert get_prompt("coder_agent", "内置默认") == "内置默认"


def test_hot_reload_on_registry_change(pdir):
    assert get_prompt("coder_agent", "") == "v1 提示词：输出 JSON。"
    (pdir / "coder_agent.v2.md").write_text("v2 提示词：新增最小化改动约束。", encoding="utf-8")
    time.sleep(0.01)
    (pdir / "registry.yaml").write_text(
        yaml.safe_dump({"version": 3, "prompts": {"coder_agent": "v2"}}, allow_unicode=True), encoding="utf-8"
    )
    # mtime 变化 → 无需重启即生效
    assert get_prompt("coder_agent", "") == "v2 提示词：新增最小化改动约束。"


def test_registry_survives_broken_yaml(pdir):
    (pdir / "registry.yaml").write_text("::: not yaml :::", encoding="utf-8")
    prompts_mod._cache = None
    assert registry(force=True)["prompts"] == {}
    assert get_prompt("coder_agent", "内置默认") == "内置默认"
