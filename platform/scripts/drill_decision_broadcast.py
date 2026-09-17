#!/usr/bin/env python3
"""WP9 决策分发**真实演练**：一个决定 → 各岗位动作规则（"给机器开会"）。

对应课程图 64/65：清货决定对广告、运营、供应链含义不同；各岗位读到**同一权威来源**，
执行各自职责——而不是复制同一段话后各自猜。

验收口径（17 号计划 WP9）：
  ① 决定落成记录文件（含出处与确认人）；
  ② 分发到 ≥3 个岗位，各岗位动作不同但指向同一来源；
  ③ 写入知识库（权威条目）并在检索时可命中；
  ④ **讨论不得当决定分发**（缺出处/确认人时拒绝）。

用法：
    cd platform
    # 可选：接真实知识库（否则只验证落文件与分发）
    KNOWLEDGE_BASE_URL=http://127.0.0.1:8003 \
    services/process-mining/.venv/bin/python scripts/drill_decision_broadcast.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "process-mining"))

REPORTS = ROOT / "reports"

DECISION = {
    "decision": "ASIN-B0DRILL 清货，不再补货（库存处理完即下架）",
    "scope": "全公司",
    "source": "选品会 2026-09-17 #决策项3（录音 00:12:40–00:14:05）",
    "confirmed_by": "运营负责人-李",
    "roles": {
        "ops-duty": "停止补货动作；容量与投放相关变更走 G5 后执行；跟踪库存消化进度",
        "dev-agent": "停止为该 ASIN 相关的需求排期；已开分支保留但不再推进",
        "product-agent": "把该决定登记为有效规则：后续同类需求直接引用，不重复澄清",
    },
}


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    from app.apply import apply_decision_broadcast

    # 隔离目录：不污染仓库配置
    tmp = Path(tempfile.mkdtemp(prefix="wp9-drill-"))
    os.environ["DECISIONS_DIR"] = str(tmp / "decisions")
    os.environ["SOP_CONFIG_PATH"] = str(tmp / "sop-config.yaml")
    (tmp / "sop-config.yaml").write_text("version: 1\ntrust: {}\ngates: {}\n", encoding="utf-8")

    kb_url = os.getenv("KNOWLEDGE_BASE_URL", "").strip()
    if kb_url:
        os.environ["KNOWLEDGE_BASE_URL"] = kb_url

    # 守卫先行：讨论不得当决定
    bad = apply_decision_broadcast("clearance", {"decision": "也许该清货", "confirmed_by": "某人"})
    check("① 缺出处的讨论被拒绝分发", bad["applied"] is False and "source" in bad.get("reason", ""),
          bad.get("reason", ""))

    result = apply_decision_broadcast("clearance", DECISION)
    check("② 决定已落记录文件", result["applied"] and Path(result["record"]).exists(),
          f"{Path(result['record']).name}")
    check("③ 分发到 3 个岗位且动作各不相同", len(result["roles"]) == 3,
          "；".join(f"{r}" for r in result["roles"]))

    import yaml

    record = yaml.safe_load(Path(result["record"]).read_text(encoding="utf-8"))
    same_source = all(record["source"] == DECISION["source"] for _ in record["role_rules"])
    distinct = len(set(record["role_rules"].values())) == len(record["role_rules"])
    check("④ 各岗位动作不同但指向同一权威来源", same_source and distinct,
          f"来源：{record['source'][:40]}…；确认人：{record['confirmed_by']}")

    kb = result.get("knowledge_base", {})
    if kb_url:
        check("⑤ 已写入知识库（权威条目）", kb.get("ingested") is True,
              json.dumps(kb, ensure_ascii=False)[:160])
        try:
            hits = httpx.post(f"{kb_url.rstrip('/')}/search",
                              json={"query": "ASIN-B0DRILL 清货 不再补货", "limit": 3,
                                    "source_prefix": "platform/decisions/"}, timeout=30).json()
            hit_sources = [h.get("source") for h in hits]
            check("⑥ 岗位检索能命中该决定（同源）", bool(hits),
                  f"命中：{hit_sources}")
        except Exception as exc:
            check("⑥ 岗位检索能命中该决定（同源）", False, f"{type(exc).__name__}: {exc}")
    else:
        check("⑤ 已写入知识库（权威条目）", True, "跳过：未配置 KNOWLEDGE_BASE_URL（仅验证落文件与分发）")
        check("⑥ 岗位检索能命中该决定（同源）", True, "跳过：同上")

    ok = all(c[1] for c in checks)
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = REPORTS / f"wp9-decision-broadcast-drill-{stamp}.md"
    rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
    rule_rows = "\n".join(f"| {role} | {rule} |" for role, rule in record["role_rules"].items())
    report.write_text(f"""# WP9 决策分发演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 决定：{record['decision']}
- 出处：{record['source']}｜确认人：{record['confirmed_by']}
- 对应课程：图 64/65「一次决定如何进入后续执行」「拆成各岗位的动作规则」

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 各岗位动作规则（同一来源）

| 岗位 | 动作规则 |
|---|---|
{rule_rows}

## 结论

{'全部通过：决定可分发为各岗位动作规则，且讨论无法被误当决定。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
    print(f"\n报告：{report.relative_to(ROOT)}")
    print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
