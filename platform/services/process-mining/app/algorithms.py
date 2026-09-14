"""标准流程挖掘算法（Process Mining）。

实现（经典流程挖掘三件套 + 一致性检查）：
1. **事件日志抽取**：`task_events`（+ 审批决策）→ (case_id, activity, timestamp)
2. **DFG（Directly-Follows Graph）**：活动频次、直接跟随边频次、开始/结束活动
3. **启发式 Miner（Heuristic Miner）**：依赖度 a⇒b = (|a>b| − |b>a|) / (|a>b| + |b>a| + 1)，
   按阈值过滤噪声边；识别长度-2 循环
4. **α-Miner**：因果（a→b）、并发（a‖b）、选择（a#b）关系；X_w 关系集 → Petri 网库所（places）
   与 start/end 变迁
5. **一致性检查（Conformance）**：
   - 启发式模型：边级一致性（trace 的每条直接跟随边是否在模型内）
   - α 网：**token replay**（标准 fitness = ½(1−missing/consumed) + ½(1−remaining/produced)）
   诚实标注：未实现 alignment-based（最优对齐）一致性；α-Miner 对噪声/短循环敏感（教科书已知局限）。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations

ACT_START = "▶"
ACT_END = "■"


# ---------------------------------------------------------------- 事件日志
def event_log_from_events(rows: list[tuple]) -> dict:
    """rows: [(case_id, activity, timestamp)] → {cases: {case_id: [activities]}, events: [...]}"""
    cases: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
    for case_id, activity, ts in rows:
        cases[str(case_id)].append((str(activity), ts))
    traces = {cid: [a for a, _ in sorted(evs, key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc))]
              for cid, evs in cases.items()}
    flat = [(cid, a, ts) for cid, evs in cases.items() for a, ts in evs]
    return {"traces": traces, "events": flat, "cases": len(traces), "events_total": len(flat)}


def extract_event_log(dsn: str, sop: str | None = None) -> dict:
    """从平台数据库抽取事件日志（任务时间线 + 审批决策）。

    `sop` 可按域过滤（异构日志按域拆分后一致性检查才有意义）。
    """
    import psycopg

    rows: list[tuple] = []
    with psycopg.connect(dsn) as conn:
        case_sop = {tid: (s or "") for tid, s in conn.execute("select id, sop from tasks").fetchall()}
        if sop:
            keep = {tid for tid, s in case_sop.items() if s == sop}
        else:
            keep = set(case_sop)
        for case_id, etype, ts in conn.execute(
            "select task_id, event_type, occurred_at from task_events order by occurred_at"
        ).fetchall():
            if case_id in keep:
                rows.append((case_id, etype, ts))
        # 审批决策作为活动（按 task_ref 关联任务）
        for case_id, gate, decision, ts in conn.execute(
            """
            select t.id, a.type, d.decision, d.decided_at
            from approval_decisions d
            join approval_requests a on a.id = d.request_id
            left join tasks t on t.external_ref = a.task_ref
            where t.id is not null
            """
        ).fetchall():
            if case_id in keep:
                rows.append((case_id, f"{gate}:{decision}", ts))
    log = event_log_from_events(rows)
    log["sop"] = sop or "(全部域)"
    log["sop_distribution"] = dict(
        Counter(case_sop.get(cid, "") for cid in log["traces"]).most_common()
    ) if not sop else {sop: log["cases"]}
    return log


# ---------------------------------------------------------------- DFG
def dfg(log: dict) -> dict:
    traces = log["traces"]
    node_freq: Counter = Counter()
    edge_freq: Counter = Counter()
    starts: Counter = Counter()
    ends: Counter = Counter()
    for trace in traces.values():
        if not trace:
            continue
        starts[trace[0]] += 1
        ends[trace[-1]] += 1
        node_freq.update(trace)
        for a, b in zip(trace, trace[1:]):
            edge_freq[(a, b)] += 1
    return {
        "nodes": [{"activity": a, "count": c} for a, c in node_freq.most_common()],
        "edges": [{"from": a, "to": b, "count": c} for (a, b), c in edge_freq.most_common()],
        "start_activities": dict(starts.most_common()),
        "end_activities": dict(ends.most_common()),
        "cases": log["cases"],
    }


# ---------------------------------------------------------------- 启发式 Miner
def heuristic_miner(log: dict, threshold: float = 0.8) -> dict:
    traces = log["traces"]
    follows: Counter = Counter()
    for trace in traces.values():
        for a, b in zip(trace, trace[1:]):
            follows[(a, b)] += 1

    activities = sorted({a for trace in traces.values() for a in trace})
    dependencies: list[dict] = []
    for a, b in combinations(activities, 2):
        ab, ba = follows.get((a, b), 0), follows.get((b, a), 0)
        if ab == 0 and ba == 0:
            continue
        value = (ab - ba) / (ab + ba + 1)
        if abs(value) >= threshold:
            dependencies.append({"from": a, "to": b, "dependency": round(value, 3), "observations": ab + ba})
    # 长度-2 循环：a⇒b 与 b⇒a 均超阈值（依赖度会互相抵消 → 用观测数判断）
    loops = [
        {"a": a, "b": b}
        for a, b in combinations(activities, 2)
        if follows.get((a, b), 0) >= 2 and follows.get((b, a), 0) >= 2
    ]
    return {
        "threshold": threshold,
        "dependencies": sorted(dependencies, key=lambda d: -d["dependency"]),
        "length2_loops": loops,
        "activities": activities,
        "cases": log["cases"],
    }


# ---------------------------------------------------------------- α-Miner
def alpha_miner(log: dict) -> dict:
    traces = [t for t in log["traces"].values() if t]
    activities = sorted({a for trace in traces for a in trace})
    follows: Counter = Counter()
    for trace in traces:
        for a, b in zip(trace, trace[1:]):
            follows[(a, b)] += 1

    causal: set[tuple[str, str]] = set()
    parallel: set[tuple[str, str]] = set()
    for a, b in combinations(activities, 2):
        ab, ba = follows.get((a, b), 0) > 0, follows.get((b, a), 0) > 0
        if ab and not ba:
            causal.add((a, b))
        elif ba and not ab:
            causal.add((b, a))
        elif ab and ba:
            parallel.add((a, b))

    # X_w：(A,B) 满足 A×B 全为因果关系，且 A、B 内部两两为选择关系
    def choice(x: str, y: str) -> bool:
        return not follows.get((x, y), 0) and not follows.get((y, x), 0)

    a_sets: list[frozenset] = []
    for size in range(1, 3):
        for combo in combinations(activities, size):
            if all(choice(x, y) for x, y in combinations(combo, 2)):
                a_sets.append(frozenset(combo))
    xw: set[tuple[frozenset, frozenset]] = set()
    for a_set in a_sets:
        for b_set in a_sets:
            if a_set == b_set or not a_set or not b_set:
                continue
            if all((a, b) in causal for a in a_set for b in b_set):
                xw.add((a_set, b_set))
    # 去除被更大关系集包含的冗余对
    places = [
        {"inputs": sorted(a_set), "outputs": sorted(b_set)}
        for a_set, b_set in xw
        if not any(a_set < a2 and b_set < b2 for a2, b2 in xw)
    ]
    start = sorted({t[0] for t in traces})
    end = sorted({t[-1] for t in traces})
    # 经典 α 算法：补充源库所（∅ → 开始活动）与汇库所（结束活动 → ∅）
    if start:
        places.append({"inputs": [], "outputs": start})
    if end:
        places.append({"inputs": end, "outputs": []})
    return {
        "activities": activities,
        "causality": [{"from": a, "to": b} for a, b in sorted(causal)],
        "parallel": [{"a": a, "b": b} for a, b in sorted(parallel)],
        "places": sorted(places, key=lambda p: (p["inputs"], p["outputs"])),
        "start_activities": start,
        "end_activities": end,
        "cases": log["cases"],
    }


# ---------------------------------------------------------------- 一致性检查
def conformance_heuristic(log: dict, net: dict) -> dict:
    """启发式模型下的边级一致性：trace 中每条直接跟随边是否在模型依赖集中。"""
    model_edges = {(d["from"], d["to"]) for d in net["dependencies"]}
    traces = log["traces"]
    conforming, deviations = 0, []
    for case_id, trace in traces.items():
        bad = [(a, b) for a, b in zip(trace, trace[1:]) if (a, b) not in model_edges]
        if bad:
            deviations.append({"case_id": case_id, "trace": trace, "missing_edges": [f"{a}→{b}" for a, b in bad][:3]})
        else:
            conforming += 1
    total = max(1, len(traces))
    return {
        "algorithm": "heuristic",
        "cases": len(traces),
        "conforming": conforming,
        "fitness": round(conforming / total, 3),
        "deviations": deviations[:10],
        "deviations_total": len(deviations),
    }


def conformance_dfg(log: dict, min_support: int = 2) -> dict:
    """DFG 留一法一致性（异构日志的异常检测，默认算法）。

    模型 = 全局观察到的直接跟随边；对每个案例做留一检验：
    其每条边在"其它案例"中是否仍有支持（support ≥ min_support）。
    只有该案例独有的路径（单例异常）才会被判偏差——避免"模型自身解释自己"。
    """
    traces = log["traces"]
    edge_counts: Counter = Counter()
    for trace in traces.values():
        for a, b in zip(trace, trace[1:]):
            edge_counts[(a, b)] += 1

    conforming, deviations = 0, []
    for case_id, trace in traces.items():
        bad = []
        for a, b in zip(trace, trace[1:]):
            support_others = edge_counts[(a, b)] - 1  # 留一：扣除本案例
            if support_others < min_support - 1:
                bad.append(f"{a}→{b}")
        if bad:
            deviations.append({"case_id": case_id, "trace": trace, "unique_edges": bad[:3]})
        else:
            conforming += 1
    total = max(1, len(traces))
    return {
        "algorithm": "dfg_leave_one_out",
        "min_support": min_support,
        "cases": len(traces),
        "conforming": conforming,
        "fitness": round(conforming / total, 3),
        "deviations": deviations[:10],
        "deviations_total": len(deviations),
    }


def threshold_sweep(log: dict, thresholds: tuple[float, ...] = (0.3, 0.5, 0.8)) -> list[dict]:
    """一致性对依赖度阈值的敏感度扫描（低阈值→更宽松模型）。"""
    out = []
    for th in thresholds:
        net = heuristic_miner(log, threshold=th)
        result = conformance_heuristic(log, net)
        out.append({"threshold": th, "model_edges": len(net["dependencies"]), "fitness": result["fitness"]})
    return out


def _alpha_trace_replay(net: dict, trace: list[str]) -> dict:
    """α 网 token replay（简化：每个变迁从其输入库所之一消费 token）。

    fitness = ½(1 − missing/consumed) + ½(1 − remaining/produced)
    """
    places = [(frozenset(p["inputs"]), frozenset(p["outputs"])) for p in net["places"]]
    marking: dict[int, int] = defaultdict(int)
    fired_inputs: dict[int, set] = defaultdict(set)
    produced = consumed = missing = 0

    # 初始：源库所（inputs 为空）注入 token
    source_places = [i for i, (ins, _) in enumerate(places) if not ins]
    for idx in source_places:
        marking[idx] += 1
        produced += 1

    for activity in trace:
        candidates = [i for i, (_, outs) in enumerate(places) if activity in outs and marking[i] > 0]
        if not candidates:
            missing += 1
            continue
        idx = candidates[0]
        marking[idx] -= 1
        consumed += 1
        for j, (ins, outs) in enumerate(places):
            if activity in ins:
                fired_inputs[j].add(activity)
                if ins <= fired_inputs[j]:
                    marking[j] += 1
                    produced += 1
                    fired_inputs[j].clear()

    # 结束：汇库所（outputs 为空）中的 token 属正常完成；其余留存视为不一致
    sink_places = {i for i, (_, outs) in enumerate(places) if not outs}
    remaining = sum(count for i, count in marking.items() if i not in sink_places)
    if not places:
        return {"fitness": 0.0, "missing": missing, "consumed": consumed, "produced": produced, "remaining": remaining}
    fitness = 0.5 * (1 - missing / max(1, consumed + missing)) + 0.5 * (1 - remaining / max(1, produced))
    return {
        "fitness": round(max(0.0, fitness), 3),
        "missing": missing,
        "consumed": consumed,
        "produced": produced,
        "remaining": remaining,
    }


def conformance_alpha(log: dict, net: dict) -> dict:
    traces = log["traces"]
    details = []
    fitness_sum = 0.0
    for case_id, trace in traces.items():
        r = _alpha_trace_replay(net, trace)
        fitness_sum += r["fitness"]
        if r["fitness"] < 1.0:
            details.append({"case_id": case_id, "trace": trace, **r})
    total = max(1, len(traces))
    return {
        "algorithm": "alpha_token_replay",
        "cases": len(traces),
        "fitness": round(fitness_sum / total, 3),
        "deviations": details[:10],
        "deviations_total": len(details),
    }
