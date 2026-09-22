"""camp-pst-20260922 缺陷回归：BUG-008/009/010（挖掘算法）、BUG-011（LLM 回退）。"""
from __future__ import annotations

import datetime

import pytest

from app.algorithms import (alpha_miner, conformance_alpha, conformance_dfg, conformance_heuristic,
                            dfg, event_log_from_events, heuristic_miner, threshold_sweep)
from app import proposals

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _log(cases: dict[str, list[str]]) -> dict:
    rows = []
    for name, acts in cases.items():
        for j, act in enumerate(acts):
            rows.append((name, act, T0 + datetime.timedelta(minutes=j)))
    return event_log_from_events(rows)


class TestBug008HeuristicDirection:
    def test_reversed_dominance_emits_correct_direction(self):
        """流程方向与字母序相反时（B→A，而 A<B），依赖方向必须是 B→A 且为正。

        原实现按 combinations() 的字母序恒定输出 A→B 并带负依赖度，
        导致模型缺失真边、自评适应度为 0（探针 ALG-001 的原始发现）。
        """
        log = _log({"c1": ["B", "A"], "c2": ["B", "A"], "c3": ["B", "A"], "c4": ["B", "A"], "c5": ["B", "A"]})
        deps = heuristic_miner(log, threshold=0.8)["dependencies"]
        assert deps and deps[0]["from"] == "B" and deps[0]["to"] == "A"
        assert deps[0]["dependency"] > 0

    def test_no_negative_dependencies_ever(self):
        log = _log({"c1": ["A", "B"], "c2": ["B", "A"], "c3": ["B", "A"]})
        assert all(d["dependency"] >= 0 for d in heuristic_miner(log, threshold=0.3)["dependencies"])

    def test_self_conformance_is_full(self):
        """模型必须能接受自己的日志（原实现自评 0.0）。"""
        log = _log({"c1": ["B", "A"], "c2": ["B", "A"], "c3": ["B", "A"], "c4": ["B", "A"], "c5": ["B", "A"]})
        net = dfg(log)
        assert conformance_heuristic(log, heuristic_miner(log))["fitness"] == 1.0
        assert net["edges"][0]["from"] == "B" and net["edges"][0]["to"] == "A"

    def test_bidirectional_pair_is_reported_as_loop_not_negative_edge(self):
        """真正的双向（length-2 循环）应进 length2_loops，而不是产出负依赖边。"""
        log = _log({"c1": ["A", "B", "A"], "c2": ["A", "B", "A"]})
        result = heuristic_miner(log, threshold=0.8)
        assert result["length2_loops"] and all(d["dependency"] >= 0 for d in result["dependencies"])


class TestBug009AlphaMiner:
    CASES = {
        "xor3": {"c1": ["A", "B", "E"], "c2": ["A", "C", "E"], "c3": ["A", "D", "E"]},
        "par3": {"c1": ["A", "B", "C", "D", "E"]},
        "loop": {"c1": ["A", "B", "A", "B", "C"]},
    }

    @pytest.mark.parametrize("name", ["xor3", "par3", "loop"])
    def test_net_replays_its_own_log(self, name):
        log = _log(self.CASES[name])
        net = alpha_miner(log)
        assert conformance_alpha(log, net)["fitness"] == 1.0, net["places"]

    def test_three_way_branch_is_compact(self):
        """三路分支应产出 4 个库所（源+2+汇），而不是把子集对全枚举出来。"""
        net = alpha_miner(_log(self.CASES["xor3"]))
        assert len(net["places"]) <= 6, net["places"]
        assert net["max_set_size"] >= 3

    def test_short_loop_places_recorded(self):
        net = alpha_miner(_log(self.CASES["loop"]))
        assert net["loop_places"] >= 2


class TestBug010DfgConformance:
    def test_unique_path_detected_with_min_support_one(self):
        log = _log({"c1": ["A", "B", "C"], "c2": ["A", "B", "C"], "c3": ["A", "B", "C"], "c4": ["A", "X", "C"]})
        r = conformance_dfg(log, min_support=1)
        assert r["deviations_total"] == 1 and r["fitness"] == 0.75

    def test_empty_cases_are_not_counted_as_conforming(self):
        log = _log({"c1": ["A", "B", "C"], "c2": ["A", "B", "C"], "c3": ["A"]})
        r = conformance_dfg(log, min_support=1)
        assert r["not_evaluable"] == ["c3"] and r["evaluable"] == 2
        assert r["fitness"] == 1.0

    def test_threshold_sweep_still_works(self):
        log = _log({"c1": ["A", "B", "C"], "c2": ["A", "B", "C"], "c3": ["A", "X", "C"]})
        assert len(threshold_sweep(log)) == 3


class TestBug011LlmFallback:
    REPORT = {"bottlenecks": [{"type": "gate_wait", "detail": "G3 waits 30h", "suggestion": "raise trust"}],
              "stats": {"requests": 10}, "gates": [], "cost_by_model": []}

    def _chat(self, payload):
        def chat(prompt):
            payload.raise_for_status()
            return payload.json()["choices"][0]["message"]["content"]
        return chat

    class _Bad:
        status_code = 200
        text = "<html>err</html>"
        def raise_for_status(self): return None
        def json(self): raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class _NoChoices:
        status_code = 200
        text = '{"ok":true}'
        def raise_for_status(self): return None
        def json(self): return {"ok": True}

    @pytest.mark.parametrize("payload_cls", ["_Bad", "_NoChoices"])
    def test_malformed_responses_fall_back(self, payload_cls):
        payload = getattr(self, payload_cls)()
        out = proposals.generate(self.REPORT, chat=self._chat(payload))
        assert isinstance(out, list) and out, "畸形响应必须回退到规则化提案"
        assert out[0]["action"]["type"] in ("advice", "trust_threshold", "gate_sla")

    def test_unknown_bottleneck_type_still_proposes(self):
        out = proposals.rule_based({"bottlenecks": [{"type": "weird_type", "detail": "something"}]})
        assert len(out) == 1 and out[0]["action"]["type"] == "advice"

    def test_rule_based_survives_missing_keys(self):
        out = proposals.rule_based({"bottlenecks": [{"type": "high_rework"}, {"type": "slow_gate"}]})
        assert len(out) == 2
