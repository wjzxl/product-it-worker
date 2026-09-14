"""流程挖掘与 SOP 自进化测试。

- 纯函数：分位数、瓶颈识别、提案解析/回退、配置应用
- 集成（需 PG）：真实挖掘报告结构
"""
import json
import os
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.apply import apply_action, config_path, record_improvement
from app.mining import _detect_bottlenecks, _percentile
from app.proposals import _parse, generate, rule_based


class TestPercentile:
    def test_basic(self):
        assert _percentile([1, 2, 3, 4, 5], 50) == 3
        assert _percentile([1, 2, 3, 4, 5], 95) == 5
        assert _percentile([], 50) is None


class TestBottlenecks:
    def test_high_rework_and_slow_gate(self):
        report = {
            "gates": [
                {"gate": "G2 方案评审", "rework_rate": 0.3, "decisions": 10, "revises": 3, "rejects": 0,
                 "wait_p50_s": 100, "wait_p95_s": 7200, "timeout_rate": 0.1},
                {"gate": "G3 代码审查", "rework_rate": 0.05, "decisions": 20, "revises": 1, "rejects": 0,
                 "wait_p50_s": 2, "wait_p95_s": 5, "timeout_rate": 0.0},
            ],
            "variants": [],
        }
        findings = _detect_bottlenecks(report)
        types = {f["type"] for f in findings}
        assert "high_rework" in types and "slow_gate" in types and "timeout" in types
        # 健康 Gate 不产生发现
        assert not any(f.get("gate") == "G3 代码审查" for f in findings)

    def test_variant_deviation(self):
        report = {
            "gates": [],
            "variants": [{"variant": "dev-domain/issue-to-merge｜task.created → status_changed", "count": 5, "avg_duration_s": 3}],
        }
        findings = _detect_bottlenecks(report)
        assert findings[0]["type"] == "variant_deviation"


class TestProposals:
    def test_parse_llm_json(self):
        raw = '{"proposals": [{"title": "t", "rationale": "r", "evidence": "e", "action": {"type": "trust_threshold", "target": "sample_ratio", "value": 0.3}, "risk": "L2"}]}'
        items = _parse(raw)
        assert items and items[0]["title"] == "t"

    def test_parse_with_fence(self):
        raw = '```json\n{"proposals": []}\n```'
        assert _parse(raw) == []

    def test_generate_marks_appliable(self):
        report = {"gates": [], "bottlenecks": [], "variants": [], "cost_by_model": [], "top_rework_reasons": []}
        chat = lambda prompt: json.dumps({"proposals": [  # noqa: E731
            {"title": "a", "rationale": "r", "evidence": "e", "action": {"type": "trust_threshold", "target": "sample_ratio", "value": 0.3}},
            {"title": "b", "rationale": "r", "evidence": "e", "action": {"type": "advice", "target": "x", "value": "y"}},
        ]}, ensure_ascii=False)
        props = generate(report, chat=chat)
        assert props[0]["appliable"] is True and props[1]["appliable"] is False
        assert props[0]["id"] == "prop-1"

    def test_generate_fallback_on_llm_error(self):
        report = {
            "gates": [{"gate": "G2 方案评审", "rework_rate": 0.25, "decisions": 8, "revises": 2, "rejects": 0,
                       "wait_p50_s": 10, "wait_p95_s": 100, "timeout_rate": 0}],
            "bottlenecks": _detect_bottlenecks({"gates": [{"gate": "G2 方案评审", "rework_rate": 0.25, "decisions": 8,
                                                          "revises": 2, "rejects": 0, "wait_p50_s": 10,
                                                          "wait_p95_s": 100, "timeout_rate": 0}], "variants": []}),
            "variants": [], "cost_by_model": [], "top_rework_reasons": [],
        }

        def boom(prompt):
            raise httpx.ConnectError("llm down")

        props = generate(report, chat=boom)
        assert props and props[0]["action"]["type"] == "advice"  # 规则化回退

    def test_rule_based_slow_gate_proposes_sample_ratio(self):
        report = {"bottlenecks": [{"type": "slow_gate", "gate": "G3 代码审查", "evidence": "p95 9000s", "hint": ""}]}
        props = rule_based(report)
        assert props[0]["action"] == {"type": "trust_threshold", "target": "sample_ratio", "value": 0.3}


class TestApply:
    @pytest.fixture()
    def cfg(self, tmp_path, monkeypatch):
        path = tmp_path / "sop-config.yaml"
        path.write_text(
            yaml.safe_dump(
                {"version": 1, "trust": {"sample_ratio": 0.2}, "gates": {"GATE_PR_REVIEW": {"sla_hours": 12}}},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("SOP_CONFIG_PATH", str(path))
        return path

    def test_apply_trust_threshold(self, cfg):
        res = apply_action({"type": "trust_threshold", "target": "sample_ratio", "value": 0.3})
        assert res["applied"] is True and res["before"] == 0.2 and res["after"] == 0.3
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["trust"]["sample_ratio"] == 0.3 and data["version"] == 2 and data["updated_at"]

    def test_apply_gate_sla(self, cfg):
        res = apply_action({"type": "gate_sla", "target": "GATE_PR_REVIEW", "value": 6})
        assert res["applied"] and res["before"] == 12 and res["after"] == 6
        assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["gates"]["GATE_PR_REVIEW"]["sla_hours"] == 6

    def test_invalid_gate_target_rejected(self, cfg):
        """非法 Gate 名（如流程变体字符串）→ 拒绝应用，不污染配置（真实演练发现的缺陷）。"""
        res = apply_action({"type": "gate_sla", "target": "product-domain/prd-flow｜task.created", "value": 20})
        assert res["applied"] is False and "非法 Gate 名称" in res["reason"]
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert not any(not k.startswith("GATE_") for k in data["gates"])

    def test_invalid_trust_key_and_bounds_rejected(self, cfg):
        assert apply_action({"type": "trust_threshold", "target": "nope", "value": 1})["applied"] is False
        assert apply_action({"type": "trust_threshold", "target": "sample_ratio", "value": 3})["applied"] is False
        assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["trust"]["sample_ratio"] == 0.2

    def test_proposal_adjustment_downgrades_bad_target(self):
        """LLM 把变体当 gate_sla 目标 → 自动降级为 advice 且标记 adjusted。"""
        report = {"gates": [], "bottlenecks": [], "variants": [], "cost_by_model": [], "top_rework_reasons": []}
        chat = lambda prompt: json.dumps({"proposals": [  # noqa: E731
            {"title": "t", "rationale": "r", "evidence": "e",
             "action": {"type": "gate_sla", "target": "product-domain/prd-flow｜task.created", "value": 20}},
        ]}, ensure_ascii=False)
        props = generate(report, chat=chat)
        assert props[0]["action"]["type"] == "advice" and props[0]["appliable"] is False
        assert "降级" in props[0]["adjusted"]

    def test_advice_not_applied(self, cfg):
        res = apply_action({"type": "advice", "target": "x", "value": "y"})
        assert res["applied"] is False and "仅登记" in res["reason"]

    def test_record_improvement(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"iid": 77, "web_url": "http://gitlab/issue/77"})

        monkeypatch.setattr("app.apply.httpx.post", lambda url, json=None, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler)).post(url, json=json))
        monkeypatch.setenv("PROJECT_ID", "42")
        res = record_improvement({"title": "t", "rationale": "r", "evidence": "e"}, {"applied": True})
        assert res["created"] and res["issue_iid"] == 77
        assert "process-improvement" in captured["body"]["labels"]


def _pg_available() -> bool:
    try:
        from app.mining import mine

        mine(days=3650)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_available(), reason="需要 PostgreSQL 平台库")
class TestMiningIntegration:
    def test_real_report_structure(self):
        from app.mining import mine

        report = mine(days=3650)
        assert {"generated_at", "gates", "variants", "bottlenecks", "cost_by_model"} <= set(report)
        if report["gates"]:
            g = report["gates"][0]
            assert {"gate", "domain", "requests", "decisions", "rework_rate", "timeout_rate"} <= set(g)
        assert isinstance(report["cost_by_model"], list)


class TestApi:
    def _client(self, monkeypatch, approval_status="approved"):
        def fake_get(url, timeout=None, **kw):
            return httpx.Response(200, json={"id": "apv_x", "status": approval_status,
                                             "decisions": [{"decided_by": "lead@corp"}]},
                                  request=httpx.Request("GET", url))

        monkeypatch.setattr("app.main.httpx.get", fake_get)
        from app.main import app

        return TestClient(app)

    def test_apply_requires_approval(self, monkeypatch, tmp_path):
        path = tmp_path / "sop-config.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "trust": {"sample_ratio": 0.2}}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setenv("SOP_CONFIG_PATH", str(path))
        client = self._client(monkeypatch, approval_status="pending")
        r = client.post("/mining/proposals/apply", json={
            "approval_id": "apv_x",
            "proposal": {"id": "prop-1", "title": "t", "action": {"type": "trust_threshold", "target": "sample_ratio", "value": 0.4}},
        })
        assert r.status_code == 409
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["trust"]["sample_ratio"] == 0.2

    def test_apply_after_approval(self, monkeypatch, tmp_path):
        path = tmp_path / "sop-config.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "trust": {"sample_ratio": 0.2}}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setenv("SOP_CONFIG_PATH", str(path))
        monkeypatch.setattr("app.main.record_improvement", lambda proposal, result: {"created": False, "reason": "test"})
        client = self._client(monkeypatch, approval_status="approved")
        r = client.post("/mining/proposals/apply", json={
            "approval_id": "apv_x",
            "proposal": {"id": "prop-1", "title": "t", "action": {"type": "trust_threshold", "target": "sample_ratio", "value": 0.4}},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["apply"]["applied"] and body["approved_by"] == "lead@corp"
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["trust"]["sample_ratio"] == 0.4

    def test_health(self, monkeypatch):
        assert self._client(monkeypatch).get("/health").json()["service"] == "process-mining"
