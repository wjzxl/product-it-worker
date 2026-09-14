"""标准流程挖掘算法测试：DFG / 启发式 Miner / α-Miner / 一致性检查。"""
from datetime import datetime, timedelta, timezone

from app.algorithms import (
    alpha_miner,
    conformance_dfg,
    conformance_alpha,
    conformance_heuristic,
    dfg,
    event_log_from_events,
    heuristic_miner,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _log(traces: dict[str, list[str]]) -> dict:
    rows = []
    for case, acts in traces.items():
        for i, a in enumerate(acts):
            rows.append((case, a, T0 + timedelta(seconds=i)))
    return event_log_from_events(rows)


SEQ = _log({"c1": ["A", "B", "C"], "c2": ["A", "B", "C"]})
# 依赖度 = (|a>b|−|b>a|)/(|a>b|+|b>a|+1)：并行分支需足够样本才能与噪声区分
PAR = _log({**{f"p{i}": ["A", "B", "C", "D"] for i in range(5)},
            **{f"q{i}": ["A", "C", "B", "D"] for i in range(5)}})
CHO = _log({"c1": ["A", "B", "D"], "c2": ["A", "C", "D"]})
LOOP = _log({"c1": ["A", "B", "A", "B", "C"], "c2": ["A", "B", "A", "B", "C"]})


class TestEventLog:
    def test_traces_sorted_by_time(self):
        rows = [("c1", "B", T0 + timedelta(seconds=5)), ("c1", "A", T0)]
        log = event_log_from_events(rows)
        assert log["traces"]["c1"] == ["A", "B"] and log["cases"] == 1 and log["events_total"] == 2


class TestDFG:
    def test_sequence_edges_and_start_end(self):
        g = dfg(SEQ)
        edges = {(e["from"], e["to"]): e["count"] for e in g["edges"]}
        assert edges == {("A", "B"): 2, ("B", "C"): 2}
        assert g["start_activities"] == {"A": 2} and g["end_activities"] == {"C": 2}
        assert {n["activity"]: n["count"] for n in g["nodes"]} == {"A": 2, "B": 2, "C": 2}


class TestHeuristicMiner:
    def test_sequence_dependencies(self):
        net = heuristic_miner(SEQ, threshold=0.5)
        deps = {(d["from"], d["to"]): d["dependency"] for d in net["dependencies"]}
        assert deps[("A", "B")] == 0.667 and deps[("B", "C")] == 0.667
        assert ("B", "A") not in deps

    def test_parallel_has_no_causal_dependency(self):
        """并发 B‖C：AB/AC 均出现 → 依赖度被抵消（<0.8 阈值），不应出现在模型边中。"""
        net = heuristic_miner(PAR, threshold=0.8)
        edges = {(d["from"], d["to"]) for d in net["dependencies"]}
        assert ("B", "C") not in edges and ("C", "B") not in edges
        assert ("A", "B") in edges and ("A", "C") in edges  # A 是并发分支前的因果前置

    def test_length2_loop_detected(self):
        net = heuristic_miner(LOOP, threshold=0.5)
        assert net["length2_loops"], "应识别 A↔B 长度-2 循环"

    def test_threshold_filters_noise(self):
        noisy = _log({"c1": ["A", "B"], "c2": ["A", "B"], "c3": ["B", "C"]})
        strict = heuristic_miner(noisy, threshold=0.9)
        loose = heuristic_miner(noisy, threshold=0.3)
        assert len(strict["dependencies"]) < len(loose["dependencies"])


class TestAlphaMiner:
    def test_sequence_causality(self):
        net = alpha_miner(SEQ)
        assert {(c["from"], c["to"]) for c in net["causality"]} == {("A", "B"), ("B", "C")}
        assert net["parallel"] == []
        assert net["start_activities"] == ["A"] and net["end_activities"] == ["C"]

    def test_parallel_relation_detected(self):
        net = alpha_miner(PAR)
        pairs = {(p["a"], p["b"]) for p in net["parallel"]}
        assert ("B", "C") in pairs
        assert {(c["from"], c["to"]) for c in net["causality"]} >= {("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")}
        # 经典 α 网：并行分支需要一个 split 库所与一个 join 库所
        places = {(tuple(p["inputs"]), tuple(p["outputs"])) for p in net["places"]}
        assert (("A",), ("B", "C")) in places or (("A",), ("B",)) in places

    def test_choice_relations_produce_places(self):
        net = alpha_miner(CHO)
        ins_outs = {(tuple(p["inputs"]), tuple(p["outputs"])) for p in net["places"]}
        assert (("A",), ("B", "C")) in ins_outs  # A 之后 B 或 C
        assert (("B", "C"), ("D",)) in ins_outs  # B 或 C 之后 D

    def test_xor_choice_no_causality_between_branches(self):
        net = alpha_miner(CHO)
        causal = {(c["from"], c["to"]) for c in net["causality"]}
        assert ("B", "C") not in causal and ("C", "B") not in causal


class TestConformanceDfg:
    def test_shared_paths_conform(self):
        """多数案例共享同一路径 → 留一法判为一致。"""
        log = _log({f"c{i}": ["task.created", "approval.requested", "approved"] for i in range(5)})
        r = conformance_dfg(log)
        assert r["fitness"] == 1.0 and r["deviations_total"] == 0

    def test_singleton_path_is_flagged(self):
        """单例异常路径（仅此案例出现）→ 判为偏差（异构日志异常检测）。"""
        traces = {f"c{i}": ["task.created", "approval.requested", "approved"] for i in range(5)}
        traces["weird"] = ["task.created", "weird_step", "approved"]
        r = conformance_dfg(_log(traces))
        assert r["fitness"] < 1.0 and r["deviations_total"] == 1
        assert r["deviations"][0]["case_id"] == "weird"
        assert any("weird_step" in e for e in r["deviations"][0]["unique_edges"])


class TestConformance:
    def test_heuristic_fitness_perfect_for_conforming(self):
        net = heuristic_miner(SEQ, threshold=0.5)
        result = conformance_heuristic(SEQ, net)
        assert result["fitness"] == 1.0 and result["deviations_total"] == 0

    def test_heuristic_detects_deviation(self):
        net = heuristic_miner(SEQ, threshold=0.5)
        deviant = _log({"c1": ["A", "B", "C"], "c2": ["A", "C"]})  # c2 缺 B → A→C 不在模型
        result = conformance_heuristic(deviant, net)
        assert result["fitness"] < 1.0 and result["deviations_total"] >= 1
        assert "A→C" in result["deviations"][0]["missing_edges"]

    def test_alpha_token_replay(self):
        net = alpha_miner(SEQ)
        result = conformance_alpha(SEQ, net)
        # 完全一致的日志在 α 网上应重放成功（fitness = 1.0）
        assert result["fitness"] == 1.0 and result["cases"] == 2 and result["deviations_total"] == 0

    def test_alpha_token_replay_penalizes_missing_activity(self):
        net = alpha_miner(SEQ)
        deviant = _log({"c1": ["A", "B", "C"], "c2": ["A", "C"]})
        result = conformance_alpha(deviant, net)
        assert result["fitness"] < conformance_alpha(SEQ, net)["fitness"]
