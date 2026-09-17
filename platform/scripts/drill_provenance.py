#!/usr/bin/env python3
"""WP1 需求来源链**真实演练**：从澄清 → G1 审批 → 反查出处的完整链路。

验收口径（17-FDE范式落地开发计划.md WP1）：
  用一条含"按月分仓"口径的需求（口径藏在语音里），展示：
    ① 澄清节点发现模糊表述并产出待确认问题（带出处）；
    ② 来源链随 G1 审批落库；
    ③ 事后从 task_ref 反查出「哪句话、哪一秒说的」；
    ④ 汇总仍缺出处的 Gate（供 E1 工程闸门消费）。
  全过程留证到 platform/reports/。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python scripts/drill_provenance.py
    # 用真实模型跑澄清（默认用确定性桩，避免每次结论漂移）：
    CLARIFY_USE_LLM=1 LLM_API_KEY=... LLM_BASE_URL=http://127.0.0.1:4000/v1 ... drill_provenance.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
TMP = tempfile.mkdtemp(prefix="wp1-drill-")
PORTS = {"approval-gateway": 8051, "agent-runtime": 8052}
REPORTS = ROOT / "reports"

REQUIREMENT = (
    "列表加个导出吧，支持常用格式就行；字段跟列表页保持一致（主要字段等）；"
    "权限按常规处理，运营也能看；数据量大的时候优化一下。"
)
# 「按月分仓」这条关键口径藏在语音里——课程图 12 的原始场景
VOICE_SOURCE = {
    "kind": "voice",
    "ref": "feishu://chat/oc_drill/msg/om_voice_214",
    "quote": "我们对账是按月分仓看的，不是按天。",
    "locator": {"start_ms": 41200, "end_ms": 47000},
    "captured_at": "2026-09-12T20:31:00+08:00",
    "confirmed_by": "李经理",
}
CHAT_SOURCE = {
    "kind": "chat",
    "ref": "feishu://chat/oc_drill/msg/om_chat_001",
    "quote": "列表加个导出吧，支持常用格式就行",
    "locator": {"msg_id": "om_chat_001"},
    "confirmed_by": "运营-张",
}
EXCEL_SOURCE = {
    "kind": "excel",
    "ref": "feishu://drive/file/月度对账.xlsx",
    "quote": "表头 11 列（含成本列）",
    "locator": {"sheet": "9月", "cell": "A1:K1"},
    "confirmed_by": "财务-王",
}


def python_of(service: str) -> str:
    return str(SERVICES / service / ".venv" / "bin" / "python")


def start() -> list[subprocess.Popen]:
    procs = []
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{TMP}/approval.db",
        "DEFAULT_TENANT": "drill",
    })
    procs.append(subprocess.Popen(
        [python_of("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["approval-gateway"])],
        cwd=SERVICES / "approval-gateway", env=env,
        stdout=open(f"{TMP}/approval.log", "w"), stderr=subprocess.STDOUT))

    env = dict(os.environ)
    env.update({
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",  # 本演练不建 issue
        "CLARIFY_USE_LLM": os.getenv("CLARIFY_USE_LLM", "0"),                  # 默认走规则检测（确定性）
        "PROMPTS_DIR": str(ROOT / "prompts"),
    })
    if os.getenv("LLM_API_KEY"):
        env.setdefault("LLM_BASE_URL", "http://127.0.0.1:4000/v1")
        env.setdefault("LLM_MODEL", "qwen-plus")
    else:
        env.pop("LLM_API_KEY", None)
    procs.append(subprocess.Popen(
        [python_of("agent-runtime"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["agent-runtime"])],
        cwd=SERVICES / "agent-runtime", env=env,
        stdout=open(f"{TMP}/runtime.log", "w"), stderr=subprocess.STDOUT))
    return procs


def wait_healthy(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok = all(
                httpx.get(f"http://127.0.0.1:{p}/health", timeout=3).status_code == 200
                for p in PORTS.values()
            )
            if ok:
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    procs = start()
    try:
        print("服务启动中…（approval-gateway + agent-runtime）")
        if not wait_healthy():
            print("❌ 服务未就绪", file=sys.stderr)
            return 1
        check("两个服务健康", True)

        task_ref = f"wp1-drill-{int(time.time())}"
        sources = [VOICE_SOURCE, CHAT_SOURCE, EXCEL_SOURCE]
        r = httpx.post(
            f"http://127.0.0.1:{PORTS['agent-runtime']}/runs",
            json={"requirement_text": REQUIREMENT, "title": "报表导出（含按月分仓口径）",
                  "task_ref": task_ref, "sources": sources},
            headers={"X-Tenant": "drill"}, timeout=60,
        )
        r.raise_for_status()
        run = r.json()
        check("① 产品域流程在 G1 挂起", run.get("status") == "waiting_approval",
              f"status={run.get('status')} approval={run.get('approval_id')}")

        clar = run.get("clarification") or {}
        check("② 澄清节点发现模糊表述", clar.get("ambiguity_count", 0) >= 3,
              f"命中 {clar.get('ambiguity_count')} 处：{[q['matched'] for q in clar.get('questions', [])]}")
        with_src = [q for q in clar.get("questions", []) if q.get("source_ref")]
        check("③ 每条待确认问题都挂出处", len(with_src) == len(clar.get("questions", [])) and len(with_src) > 0,
              f"{len(with_src)}/{len(clar.get('questions', []))} 条带出处")
        check("④ 明示默认值清单已生成", len(clar.get("defaults_to_confirm", [])) >= 4,
              f"{len(clar.get('defaults_to_confirm', []))} 项（含时区/空值/精度/上限）")

        # 来源链随审批落库
        approval_id = run.get("approval_id", "")
        apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}",
                        headers={"X-Tenant": "drill"}, timeout=30).json()
        stored = (apv.get("context_snapshot") or {}).get("sources") or []
        check("⑤ 来源链随 G1 审批落库", len(stored) == len(sources),
              f"落库 {len(stored)} 条（voice/chat/excel）")

        # 反查：产物 → 出处
        prov = httpx.get(
            f"http://127.0.0.1:{PORTS['approval-gateway']}/requirements/{task_ref}/provenance",
            headers={"X-Tenant": "drill"}, timeout=30,
        ).json()
        voice = next((s for s in prov.get("sources", []) if s["kind"] == "voice"), None)
        check("⑥ 反查到语音出处与时间戳", bool(voice) and voice["locator"].get("start_ms") == 41200,
              f"{voice['ref'] if voice else '-'} @ {voice['locator'] if voice else '-'}")
        check("⑦ 反查到「按月分仓」原话", bool(voice) and "按月分仓" in voice.get("quote", ""),
              f"quote={voice.get('quote') if voice else '-'}")
        check("⑧ 出处带确认人（谁说的）", bool(voice) and voice.get("confirmed_by") == "李经理",
              f"confirmed_by={voice.get('confirmed_by') if voice else '-'}")
        check("⑨ 按载体汇总正确", prov.get("counts_by_kind") == {"chat": 1, "voice": 1, "excel": 1},
              json.dumps(prov.get("counts_by_kind"), ensure_ascii=False))
        check("⑩ 无缺出处的 Gate", prov.get("gates_without_sources") == [],
              f"gates_without_sources={prov.get('gates_without_sources')}")

        # 跨产品线不可见（隔离）
        other = httpx.get(
            f"http://127.0.0.1:{PORTS['approval-gateway']}/requirements/{task_ref}/provenance",
            headers={"X-Tenant": "line-b"}, timeout=30,
        )
        check("⑪ 跨产品线查不到（租户隔离）", other.status_code == 404, f"HTTP {other.status_code}")

        ok = all(c[1] for c in checks)
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"wp1-provenance-drill-{stamp}.md"
        rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
        report.write_text(
            f"""# WP1 需求来源链演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 需求原文：{REQUIREMENT}
- 澄清方式：{'LLM（clarify_agent，注册中心）' if os.getenv('CLARIFY_USE_LLM') == '1' else '规则检测（确定性，CLARIFY_USE_LLM=0）'}
- task_ref：`{task_ref}`｜审批单：`{approval_id}`

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 反查链路（产物 → 出处）

```
需求「报表导出」→ task_ref {task_ref}
  └─ 关键口径：{VOICE_SOURCE['quote']}
       kind={VOICE_SOURCE['kind']} ref={VOICE_SOURCE['ref']}
       locator={json.dumps(VOICE_SOURCE['locator'], ensure_ascii=False)}
       confirmed_by={VOICE_SOURCE['confirmed_by']}
  └─ 素材：chat {CHAT_SOURCE['ref']}｜excel {EXCEL_SOURCE['ref']}
```

## 结论

{'全部通过：来源链可从产物反查到语音时间戳与原话。' if ok else '存在未通过项，见上表。'}
""",
            encoding="utf-8",
        )
        print(f"\n报告：{report.relative_to(ROOT)}")
        print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        for p in procs:
            p.terminate()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
