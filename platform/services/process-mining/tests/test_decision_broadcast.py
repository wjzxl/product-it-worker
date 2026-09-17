"""WP9 决策分发测试（课程图 64/65：「给机器开会」）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from app.apply import DEFAULT_ROLE_MAP, apply_action, apply_decision_broadcast


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """决策文件与 SOP 配置都落临时目录，避免污染仓库。"""
    (tmp_path / "sop-config.yaml").write_text("version: 1\ntrust: {}\ngates: {}\n", encoding="utf-8")
    monkeypatch.setenv("SOP_CONFIG_PATH", str(tmp_path / "sop-config.yaml"))
    monkeypatch.setenv("DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.delenv("KNOWLEDGE_BASE_URL", raising=False)
    yield


DECISION = {
    "decision": "ASIN-B0TEST 清货，不再补货",
    "scope": "全公司",
    "source": "选品会 2026-09-17 #决策项3",
    "confirmed_by": "运营负责人",
}


def test_broadcast_writes_record_with_all_roles():
    result = apply_decision_broadcast("clearance", DECISION)
    assert result["applied"] is True and result["authoritative"] is True
    assert set(result["roles"]) == set(DEFAULT_ROLE_MAP)
    record = yaml.safe_load(Path(result["record"]).read_text(encoding="utf-8"))
    assert record["decision"] == DECISION["decision"]
    assert record["source"] == DECISION["source"] and record["confirmed_by"] == "运营负责人"
    assert len(record["role_rules"]) == len(DEFAULT_ROLE_MAP)


def test_role_specific_rules_override_defaults():
    value = {**DECISION, "roles": {"ops-duty": "停止补货并盘点库存", "new-role": "自定义动作"}}
    result = apply_decision_broadcast("clearance", value)
    record = yaml.safe_load(Path(result["record"]).read_text(encoding="utf-8"))
    assert record["role_rules"]["ops-duty"] == "停止补货并盘点库存"
    assert record["role_rules"]["new-role"] == "自定义动作"
    assert "dev-agent" in record["role_rules"]            # 未覆盖的岗位仍拿到默认规则


def test_discussion_cannot_be_broadcast_as_decision():
    """守卫：没有出处/确认人的内容不得当决定分发（课程整理稿明确提醒）。"""
    no_source = apply_decision_broadcast("x", {"decision": "也许该清货", "confirmed_by": "某人"})
    assert no_source["applied"] is False and "source" in no_source["reason"]
    no_confirmer = apply_decision_broadcast("x", {"decision": "清货", "source": "会议"})
    assert no_confirmer["applied"] is False and "confirmed_by" in no_confirmer["reason"]
    empty = apply_decision_broadcast("x", {})
    assert empty["applied"] is False


def test_apply_action_dispatch():
    result = apply_action({"type": "decision_broadcast", "target": "clearance", "value": DECISION})
    assert result["applied"] is True and result["kind"] == "decision_broadcast"


def test_knowledge_base_failure_is_reported_not_hidden(monkeypatch):
    """KB 不可用时如实回报（决定已落文件，但知识分发失败要看得见）。"""
    monkeypatch.setenv("KNOWLEDGE_BASE_URL", "http://127.0.0.1:1")
    result = apply_decision_broadcast("clearance", DECISION)
    assert result["applied"] is True
    assert result["knowledge_base"]["ingested"] is False
    assert "error" in result["knowledge_base"]


def test_action_is_appliable_for_proposals():
    from app.proposals import APPLIABLE_ACTIONS
    assert "decision_broadcast" in APPLIABLE_ACTIONS
