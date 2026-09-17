"""WP2 工程闸门 E1 测试。

验收口径（17 号文档 WP2）：
  ① 「4/5 通过但 critical 项缺失 → 仍拒绝」；
  ② `ENG_GATES_MODE=warn` 时只记录不阻断（灰度）；
  ③ 判定留痕可查（含规则版本/指纹）；
  ④ 上下文自动补齐（来源链从审批记录取，不要求调用方记得传）；
  ⑤ 租户隔离；
  ⑥ 规则文件非法时报错，而不是静默降级。
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="wp2-gates-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app import gate_rules  # noqa: E402
from app.main import app  # noqa: E402
from app.models import utcnow  # noqa: E402

VOICE_SOURCE = {
    "kind": "voice", "ref": "feishu://chat/oc/msg/om_v1",
    "quote": "我们对账是按月分仓看的，不是按天。",
    "locator": {"start_ms": 41200, "end_ms": 47000}, "confirmed_by": "李经理",
}

# 满足除 impact_scope 以外的全部检查项（含 critical 那一项缺失）
FULL_CONTEXT_EXCEPT_IMPACT = {
    "prd_ref": "PRD v1",
    "sources": [VOICE_SOURCE],
    "change_targets": ["src/report/export.py", "src/api/report.py"],
    "rollback_plan": "保留上一版本，按灰度指标验证后一键切回",
}
FULL_CONTEXT = {**FULL_CONTEXT_EXCEPT_IMPACT, "impact_scope": "复用现有 /api/v1/report/export，不改表结构"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestNewSemantics:
    """A1–A4：业务确认后的细化判定。"""

    BASE = {
        "prd_ref": "PRD v1", "change_targets": ["src/a.py"], "impact_scope": "仅新增",
        "rollback_plan": "切回上一版本", "sources": [VOICE_SOURCE],
    }

    def test_business_source_requires_business_carrier(self):
        """A1：人工录入不算业务依据。"""
        ctx = dict(self.BASE, sources=[{"kind": "manual", "ref": "x", "confirmed_by": "张三"}])
        result = gate_rules.evaluate(gate_rules.load_rule("E1"), ctx, mode_override="block")
        gap = next(g for g in result["gaps"] if g["id"] == "business_source")
        assert "业务方载体" in gap["detail"] and result["blocked"] is True

    def test_business_source_requires_confirmer(self):
        ctx = dict(self.BASE, sources=[{"kind": "voice", "ref": "v1", "quote": "按月分仓"}])
        result = gate_rules.evaluate(gate_rules.load_rule("E1"), ctx, mode_override="block")
        gap = next(g for g in result["gaps"] if g["id"] == "business_source")
        assert "confirmed_by" in gap["detail"]

    def test_tech_only_exempts_requirement_basis_with_reason(self):
        """A2：纯技术改动豁免需求依据，但必须写明原因。"""
        base = {k: v for k, v in self.BASE.items() if k not in ("prd_ref", "sources")}
        exempt_ok = gate_rules.evaluate(gate_rules.load_rule("E1"),
                                        {**base, "tech_only": True,
                                         "tech_only_reason": "依赖安全升级，无业务变更"},
                                        mode_override="block")
        assert exempt_ok["passed"] is True, exempt_ok["gaps"]
        missing_reason = gate_rules.evaluate(gate_rules.load_rule("E1"), {**base, "tech_only": True},
                                             mode_override="block")
        gap = next(g for g in missing_reason["gaps"] if g["id"] == "requirement_basis")
        assert "tech_only_reason" in gap["detail"]

    def test_migration_rollback_only_triggers_on_migration(self):
        """A3：不涉及迁移时不适用；涉及则必须有回滚脚本。"""
        rule = gate_rules.load_rule("E1")
        not_applicable = gate_rules.evaluate(rule, self.BASE, mode_override="block")
        assert not_applicable["passed"] is True  # 未声明迁移 → 该项不适用
        triggered = gate_rules.evaluate(rule, {**self.BASE, "data_migration": True}, mode_override="block")
        gap = next(g for g in triggered["gaps"] if g["id"] == "migration_rollback")
        assert "data_migration" in gap["detail"] and triggered["blocked"] is True
        ok = gate_rules.evaluate(rule, {**self.BASE, "data_migration": True,
                                        "migration_rollback_script": "migrations/001.up.sql + down.sql"},
                                 mode_override="block")
        assert ok["passed"] is True

    def test_pure_addition_exempts_rollback_but_needs_offline_plan(self):
        """A4：纯新增豁免回滚方案，但仍需说明如何下线。"""
        rule = gate_rules.load_rule("E1")
        base = {k: v for k, v in self.BASE.items() if k != "rollback_plan"}
        exempt_ok = gate_rules.evaluate(rule, {**base, "is_pure_addition": True,
                                               "offline_plan": "下掉入口即可，无数据残留"},
                                        mode_override="block")
        assert exempt_ok["passed"] is True, exempt_ok["gaps"]
        no_plan = gate_rules.evaluate(rule, {**base, "is_pure_addition": True}, mode_override="block")
        gap = next(g for g in no_plan["gaps"] if g["id"] == "rollback_plan")
        assert "如何下线" in gap["detail"] or "offline_plan" in gap["detail"]


class TestRuleLoading:
    def test_e1_rule_loads_with_expected_checks(self):
        rule = gate_rules.load_rule("E1")
        ids = [c.id for c in rule.checks]
        assert ids == ["requirement_basis", "business_source", "change_targets",
                       "impact_scope", "migration_rollback", "rollback_plan"]
        # A1/A3：来源质量与迁移回滚都是 critical
        assert [c.id for c in rule.checks if c.severity == "critical"] == [
            "business_source", "impact_scope", "migration_rollback"]
        assert rule.mode == "block"
        assert rule.fingerprint()

    def test_fingerprint_changes_with_rule_content(self):
        raw = yaml.safe_load(open(gate_rules.rule_dir() / "E1-pre-implementation.yaml", encoding="utf-8"))
        base = gate_rules.parse_rule(raw)
        raw2 = dict(raw)
        raw2["mode"] = "warn"
        assert gate_rules.parse_rule(raw2).fingerprint() != base.fingerprint()

    def test_invalid_rule_rejected_with_location(self, tmp_path):
        bad = tmp_path / "E1-bad.yaml"
        bad.write_text(yaml.safe_dump({"code": "E1", "mode": "maybe", "checks": [{"id": "x", "ask": "y"}]}), encoding="utf-8")
        with pytest.raises(gate_rules.GateRuleError) as exc:
            gate_rules.load_rule("E1", directory=tmp_path)
        assert "mode 必须是" in str(exc.value)

    def test_missing_rule_reports_directory(self, tmp_path):
        with pytest.raises(gate_rules.GateRuleError) as exc:
            gate_rules.load_rule("E9", directory=tmp_path)
        assert "未找到闸门规则" in str(exc.value)


class TestEvaluation:
    def test_all_pass(self):
        rule = gate_rules.load_rule("E1")
        result = gate_rules.evaluate(rule, FULL_CONTEXT, mode_override="block")
        assert result["passed"] is True and result["blocked"] is False and result["gaps"] == []

    def test_four_of_five_passed_but_critical_missing_still_blocked(self):
        """核心验收：关键未知不能被多数通过抵消（课程图 40）。"""
        rule = gate_rules.load_rule("E1")
        result = gate_rules.evaluate(rule, FULL_CONTEXT_EXCEPT_IMPACT, mode_override="block")
        assert result["passed"] is False
        assert result["blocked"] is True
        assert result["critical_missing"] == ["impact_scope"]   # 来源已带确认人，故只缺影响范围
        passed_ids = [r["id"] for r in result["results"] if r["passed"]]
        assert len(passed_ids) == 5, passed_ids        # 6 项中 5 项通过（仅缺影响范围）
        assert "关键未知不能被多数通过抵消" in gate_rules.render_gaps_text(result)

    def test_warn_mode_records_without_blocking(self):
        rule = gate_rules.load_rule("E1")
        result = gate_rules.evaluate(rule, {}, mode_override="warn")
        assert result["passed"] is False and result["blocked"] is False
        assert result["mode"] == "warn"
        assert "仅记录" in gate_rules.render_gaps_text(result)

    def test_off_mode_is_not_blocking(self):
        rule = gate_rules.load_rule("E1")
        assert gate_rules.evaluate(rule, {}, mode_override="off")["blocked"] is False

    def test_global_default_mode_from_env(self, monkeypatch):
        rule = gate_rules.load_rule("E1")
        monkeypatch.delenv("ENG_GATES_MODE", raising=False)
        assert gate_rules.evaluate(rule, {})["mode"] == "block"   # 未设置 → 用规则自身声明
        monkeypatch.setenv("ENG_GATES_MODE", "warn")
        assert gate_rules.evaluate(rule, {})["mode"] == "warn"    # 全局降级生效
        monkeypatch.setenv("ENG_GATES_MODE", "off")
        assert gate_rules.evaluate(rule, {})["mode"] == "off"
        # 配置笔误不能让流水线被硬阻断：非法值按 warn 处理（并告警）
        monkeypatch.setenv("ENG_GATES_MODE", "随便写的")
        assert gate_rules.evaluate(rule, {})["mode"] == "warn"
        assert gate_rules.resolve_mode("block", "block") == "block"
        assert gate_rules.resolve_mode("block", "非法") == "warn"

    def test_gaps_carry_actionable_hint(self):
        rule = gate_rules.load_rule("E1")
        result = gate_rules.evaluate(rule, {}, mode_override="block")
        assert all(g.get("hint") for g in result["gaps"]), "每个缺口都要给出补齐建议"


class TestApi:
    def test_check_endpoint_blocks_and_persists(self, client):
        r = client.post("/eng-gates/E1/check", json={"task_ref": "wp2-a", "context": FULL_CONTEXT_EXCEPT_IMPACT})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["blocked"] is True and body["code"] == "E1"
        assert body["check_id"].startswith("gck_")
        assert body["rule_version"] and body["fingerprint"]

        hist = client.get("/eng-gates/checks", params={"task_ref": "wp2-a"}).json()
        assert len(hist) == 1 and hist[0]["blocked"] is True
        assert hist[0]["gap_ids"] == ["impact_scope"]

    def test_context_auto_filled_from_approval(self, client):
        """只给 task_ref，来源链/prd_ref 从审批记录自动补齐 → 少两项缺口。"""
        payload = {
            "type": "GATE_REQUIREMENT_REVIEW", "stage": "产品域 G1", "requester": "产品Agent@wp2-b",
            "task_ref": "wp2-b",
            "context_snapshot": {
                "artifact_ref": "PRD v1", "diff_summary": "导出需求 PRD",
                "evidence": ["https://example.com/prd"], "sources": [VOICE_SOURCE],
                "risk": {"score": 20, "level": "L1", "factors": ["常规"]},
            },
            "deadline": (utcnow() + timedelta(hours=2)).isoformat(),
            "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
        }
        assert client.post("/approvals", json=payload).status_code == 201

        body = client.post("/eng-gates/E1/check", json={"task_ref": "wp2-b", "context": {}}).json()
        gap_ids = [g["id"] for g in body["gaps"]]
        assert "requirement_basis" not in gap_ids, "prd_ref 应从审批自动补齐"
        assert "business_source" not in gap_ids, "业务来源应从审批自动补齐（含确认人）"
        assert set(gap_ids) == {"change_targets", "impact_scope", "rollback_plan"}

    def test_mode_override_warn(self, client):
        body = client.post("/eng-gates/E1/check", json={"task_ref": "wp2-c", "context": {}, "mode": "warn"}).json()
        assert body["mode"] == "warn" and body["blocked"] is False
        hist = client.get("/eng-gates/checks", params={"task_ref": "wp2-c"}).json()
        assert hist[0]["mode"] == "warn" and hist[0]["passed"] is False

    def test_unknown_gate_404(self, client):
        assert client.post("/eng-gates/E9/check", json={"context": {}}).status_code == 404

    def test_rules_listing_exposes_fingerprint(self, client):
        rules = client.get("/eng-gates").json()
        e1 = next(r for r in rules if r["code"] == "E1")
        assert e1["fingerprint"] and e1["checks"]

    def test_tenant_isolation_of_history(self, client):
        client.post("/eng-gates/E1/check", json={"task_ref": "wp2-tenant", "context": {}},
                    headers={"X-Tenant": "line-a"})
        assert client.get("/eng-gates/checks", params={"task_ref": "wp2-tenant"},
                          headers={"X-Tenant": "line-b"}).json() == []
        assert len(client.get("/eng-gates/checks", params={"task_ref": "wp2-tenant"},
                              headers={"X-Tenant": "line-a"}).json()) == 1


# --------------------------------------------------------------------------------------
# WP3：E2 解构后 / E3 交付前
# --------------------------------------------------------------------------------------
def _design_ok(**overrides) -> dict:
    """一个"五份齐全"的模块设计；overrides 用于制造缺口。"""
    facets = {
        "data": {"tables": ["report_export_log"]},
        "business": {"rules": ["超过 5000 行转后台"]},
        "api": {"path": "POST /api/v1/report/export"},
        "frontend": {"page": "列表页右上角导出按钮"},
        "test": {"cases": ["权限隔离", "行数一致"]},
    }
    facets.update(overrides)
    return {"report": facets}


class TestE2:
    def test_e2_rule_has_two_critical_checks(self):
        rule = gate_rules.load_rule("E2")
        crit = [c.id for c in rule.checks if c.severity == "critical"]
        assert crit == ["design_docs_complete", "ux_states_defined"]

    def test_design_complete_passes(self):
        rule = gate_rules.load_rule("E2")
        ctx = {"design_docs": _design_ok(),
               "ux_states": {"empty": "空态文案", "error": "可重试提示", "loading": "骨架屏"},
               "api_contract_ref": "project-rules/constraints/api-protocol.md",
               "data_dictionary_ref": "project-rules/design/report/data.md"}
        result = gate_rules.evaluate(rule, ctx, mode_override="block")
        assert result["passed"] is True, result["gaps"]

    def test_missing_frontend_without_declaration_is_blocked(self):
        """课程图 41：界面未定义必须打回。"""
        ctx = {"design_docs": _design_ok(frontend=None),
               "ux_states": {"empty": "x", "error": "y", "loading": "z"},
               "api_contract_ref": "a", "data_dictionary_ref": "b"}
        result = gate_rules.evaluate(gate_rules.load_rule("E2"), ctx, mode_override="block")
        assert result["blocked"] is True
        gap = next(g for g in result["gaps"] if g["id"] == "design_docs_complete")
        assert "report.frontend" in gap["detail"]

    def test_not_applicable_with_reason_is_accepted(self):
        """课程图 30：区分「经过确认的空」与「漏掉的空」。"""
        na = {"not_applicable": True, "reason": "后台任务，无界面"}
        ctx = {"design_docs": _design_ok(frontend={"not_applicable": True, "reason": "后台任务，无页面"}),
               "ux_states": {"empty": na, "error": na, "loading": na},
               "api_contract_ref": "a", "data_dictionary_ref": "b"}
        result = gate_rules.evaluate(gate_rules.load_rule("E2"), ctx, mode_override="block")
        assert result["passed"] is True, result["gaps"]

    def test_not_applicable_without_reason_is_blocked(self):
        ctx = {"design_docs": _design_ok(frontend={"not_applicable": True, "reason": "  "}),
               "ux_states": {"empty": "x", "error": "y", "loading": "z"},
               "api_contract_ref": "a", "data_dictionary_ref": "b"}
        result = gate_rules.evaluate(gate_rules.load_rule("E2"), ctx, mode_override="block")
        assert result["blocked"] is True
        assert "未写原因" in gate_rules.render_gaps_text(result)

    def test_ux_states_missing_is_blocked(self):
        ctx = {"design_docs": _design_ok(), "ux_states": {"empty": "空态"},
               "api_contract_ref": "a", "data_dictionary_ref": "b"}
        result = gate_rules.evaluate(gate_rules.load_rule("E2"), ctx, mode_override="block")
        gap = next(g for g in result["gaps"] if g["id"] == "ux_states_defined")
        assert "error" in gap["detail"] and "loading" in gap["detail"]


class TestE3:
    FULL = {
        "artifacts": ["src/report/export.py", "migrations/001.sql", "docs/report-export.md"],
        "acceptance_items": [
            {"id": "acc-1", "evidence": ["export-sample.csv"]},
            {"id": "acc-2", "evidence": ["log-permission.txt"]},
        ],
        "plan_consistency": "与方案一致，无偏离",
        "handover": "README + 回滚步骤",
    }

    def test_prior_gates_missing_blocks(self):
        """不允许跳着交：没有 E1/E2 记录时直接阻断。"""
        result = gate_rules.evaluate(gate_rules.load_rule("E3"), dict(self.FULL), mode_override="block")
        assert result["blocked"] is True
        gap = next(g for g in result["gaps"] if g["id"] == "prior_gates_passed")
        assert "未执行" in gap["detail"]

    def test_all_pass_when_prior_gates_and_evidence_present(self):
        ctx = dict(self.FULL, prior_gates={"E1": {"passed": True}, "E2": {"passed": True}})
        result = gate_rules.evaluate(gate_rules.load_rule("E3"), ctx, mode_override="block")
        assert result["passed"] is True, result["gaps"]

    def test_six_artifacts_five_evidences_still_blocked(self):
        """课程图 42：产物 6/6、证据 5/6 仍不算完成。"""
        items = [{"id": f"acc-{i}", "evidence": ["e"]} for i in range(1, 6)]
        items.append({"id": "acc-6"})  # 第 6 项没有证据
        ctx = dict(self.FULL, acceptance_items=items,
                   prior_gates={"E1": {"passed": True}, "E2": {"passed": True}})
        result = gate_rules.evaluate(gate_rules.load_rule("E3"), ctx, mode_override="block")
        assert result["blocked"] is True
        gap = next(g for g in result["gaps"] if g["id"] == "acceptance_evidence")
        assert "acc-6" in gap["detail"]

    def test_failed_prior_gate_blocks(self):
        ctx = dict(self.FULL, prior_gates={"E1": {"passed": True}, "E2": {"passed": False}})
        result = gate_rules.evaluate(gate_rules.load_rule("E3"), ctx, mode_override="block")
        gap = next(g for g in result["gaps"] if g["id"] == "prior_gates_passed")
        assert "未通过" in gap["detail"] and "E2" in gap["detail"]

    def test_evidence_auto_filled_from_gate_history(self, client):
        """端到端：E3 的 prior_gates 从真实检查历史自动补齐（不需调用方传）。"""
        ctx = {"prd_ref": "PRD v1",
               "sources": [VOICE_SOURCE], "change_targets": ["src/a.py"],
               "impact_scope": "仅新增导出接口", "rollback_plan": "切回"}
        assert client.post("/eng-gates/E1/check", json={"task_ref": "wp3-chain", "context": ctx}).json()["passed"]
        design = {"design_docs": _design_ok(),
                  "ux_states": {"empty": "空态", "error": "报错", "loading": "加载"},
                  "api_contract_ref": "a", "data_dictionary_ref": "b"}
        assert client.post("/eng-gates/E2/check", json={"task_ref": "wp3-chain", "context": design}).json()["passed"]

        e3 = client.post("/eng-gates/E3/check", json={
            "task_ref": "wp3-chain",
            "context": {"artifacts": ["a"], "acceptance_items": [{"id": "acc-1", "evidence": ["e"]}],
                        "plan_consistency": "一致", "handover": "README"},
        }).json()
        assert e3["passed"] is True, e3["gaps"]
        gap_ids = [g["id"] for g in e3["gaps"]]
        assert "prior_gates_passed" not in gap_ids

    def test_evidence_not_yet_recorded_reports_missing_evidence(self, client):
        """未补齐证据时，E3 明确报"证据缺失"（WP5 落地后接真实证据表）。"""
        ctx = {"artifacts": ["a"], "acceptance_items": [{"id": "acc-1"}, {"id": "acc-2", "evidence": ["e"]}],
               "plan_consistency": "一致", "handover": "README"}
        e3 = client.post("/eng-gates/E3/check", json={"task_ref": "wp3-missing", "context": ctx}).json()
        assert e3["blocked"] is True
        detail = next(g["detail"] for g in e3["gaps"] if g["id"] == "acceptance_evidence")
        assert "acc-1" in detail


class TestFeedbackReliability:
    """C3/C4：样本不足时不得用误拦率做决策；达标后给出处置建议。

    每个用例用**独立租户**隔离（共享库下统计按 X-Tenant 过滤），避免相互污染。
    """

    @staticmethod
    def _seed(tenant: str, prefix: str, n_blocked: int, n_judged: int, n_false: int):
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.models import GateCheck

        with Session(engine) as db:
            for i in range(n_blocked):
                db.add(GateCheck(tenant=tenant, code="E1", task_ref=f"{prefix}-{i}",
                                 mode="warn", passed=False, blocked=True,
                                 gaps=[{"id": "impact_scope"}], rule_version="v2"))
            db.commit()
            rows = db.query(GateCheck).filter(GateCheck.task_ref.like(f"{prefix}-%")).all()
            for i, row in enumerate(rows[:n_judged]):
                row.feedback = "false_positive" if i < n_false else "valid"
            db.commit()

    def test_insufficient_samples_are_not_reliable(self, client):
        self._seed("fb-small", "fb-small-task", n_blocked=3, n_judged=2, n_false=1)
        stats = client.get("/eng-gates/feedback/stats", headers={"X-Tenant": "fb-small"}).json()
        assert stats["blocked_total"] == 3 and stats["judged"] == 2
        assert stats["reliable"] is False
        assert any("门槛" in r for r in stats["unreliable_reasons"])
        assert "样本不足" in stats["recommended_action"]

    def test_high_false_positive_rate_recommends_rollback(self, client, monkeypatch):
        monkeypatch.setenv("GATE_MIN_JUDGED", "5")
        monkeypatch.setenv("GATE_MIN_COVERAGE", "0.8")
        import importlib

        from app import eng_gates as eg
        importlib.reload(eg)
        try:
            self._seed("fb-big", "fb-big-task", n_blocked=20, n_judged=16, n_false=4)
            stats = client.get("/eng-gates/feedback/stats", headers={"X-Tenant": "fb-big"}).json()
            assert stats["judged"] == 16 and stats["coverage"] >= 0.8
            assert stats["reliable"] is True
            assert stats["false_positive_rate"] == 0.25
            assert "退回 warn" in stats["recommended_action"]
        finally:
            monkeypatch.delenv("GATE_MIN_JUDGED", raising=False)
            monkeypatch.delenv("GATE_MIN_COVERAGE", raising=False)
            importlib.reload(eg)

    def test_coverage_below_threshold_blocks_reliability(self, client, monkeypatch):
        monkeypatch.setenv("GATE_MIN_JUDGED", "5")
        monkeypatch.setenv("GATE_MIN_COVERAGE", "0.8")
        import importlib

        from app import eng_gates as eg
        importlib.reload(eg)
        try:
            self._seed("fb-lowcov", "fb-lowcov-task", n_blocked=20, n_judged=6, n_false=0)
            stats = client.get("/eng-gates/feedback/stats", headers={"X-Tenant": "fb-lowcov"}).json()
            assert stats["reliable"] is False
            assert any("覆盖率" in r for r in stats["unreliable_reasons"])
        finally:
            monkeypatch.delenv("GATE_MIN_JUDGED", raising=False)
            monkeypatch.delenv("GATE_MIN_COVERAGE", raising=False)
            importlib.reload(eg)

    def test_low_false_positive_rate_allows_keeping_rules(self, client, monkeypatch):
        monkeypatch.setenv("GATE_MIN_JUDGED", "5")
        monkeypatch.setenv("GATE_MIN_COVERAGE", "0.8")
        import importlib

        from app import eng_gates as eg
        importlib.reload(eg)
        try:
            self._seed("fb-good", "fb-good-task", n_blocked=20, n_judged=18, n_false=1)
            stats = client.get("/eng-gates/feedback/stats", headers={"X-Tenant": "fb-good"}).json()
            assert stats["reliable"] is True and stats["false_positive_rate"] < 0.1
            assert "维持当前规则" in stats["recommended_action"]
        finally:
            monkeypatch.delenv("GATE_MIN_JUDGED", raising=False)
            monkeypatch.delenv("GATE_MIN_COVERAGE", raising=False)
            importlib.reload(eg)
