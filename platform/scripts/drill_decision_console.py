#!/usr/bin/env python3
"""WP10 驾驶舱**真实演练**：待决策队列 → 决策提交 → 误拦反馈闭环。

对应课程图 67/68：
  - 驾驶舱显示"卡在哪里、什么需要自己决定"，并能**点击直达上下文**；
  - 本演练同时验证 M1 决策 D3：误拦反馈字段让"误拦率"变成可计算指标。

验收口径（17 号计划 WP10）：
  ① 队列聚合三个来源：待审审批（业务决策）、闸门阻断（实施条件缺口）、等待中的运行；
  ② 每条都带"为什么卡住 + 可执行动作 + 深链"；
  ③ 从驾驶舱提交决策 → 走审批网关留痕（不绕过审计）；
  ④ 误拦反馈可提交，且 `feedback/stats` 的误拦率随之变化。

用法：
    cd platform
    services/metrics-dashboard/.venv/bin/python scripts/drill_decision_console.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
TMP = tempfile.mkdtemp(prefix="wp10-drill-")
GATEWAY_PORT = 8051
CONSOLE_PORT = 8056
REPORTS = ROOT / "reports"


def python_of(service: str) -> str:
    return str(SERVICES / service / ".venv" / "bin" / "python")


def start_gateway() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{TMP}/gateway.db", "DEFAULT_TENANT": "drill"})
    return subprocess.Popen(
        [python_of("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(GATEWAY_PORT)],
        cwd=SERVICES / "approval-gateway", env=env,
        stdout=open(f"{TMP}/gateway.log", "w"), stderr=subprocess.STDOUT,
    )


def start_console() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{GATEWAY_PORT}",
                "AGENT_RUNTIME_URL": "http://127.0.0.1:1"})   # 运行归档故意不可用，验证容错
    return subprocess.Popen(
        [python_of("metrics-dashboard"), "-m", "uvicorn", "app.main:app", "--port", str(CONSOLE_PORT)],
        cwd=SERVICES / "metrics-dashboard", env=env,
        stdout=open(f"{TMP}/console.log", "w"), stderr=subprocess.STDOUT,
    )


def wait_healthy(procs_ports: list[tuple[subprocess.Popen, int]], timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if all(httpx.get(f"http://127.0.0.1:{p}/health", timeout=3).status_code == 200
                   for _proc, p in procs_ports):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    gateway = start_gateway()
    console_proc = start_console()
    gbase = f"http://127.0.0.1:{GATEWAY_PORT}"
    cbase = f"http://127.0.0.1:{CONSOLE_PORT}"
    hdr = {"X-Tenant": "drill"}
    try:
        print("启动中（approval-gateway + metrics-dashboard 驾驶舱）…")
        if not wait_healthy([(gateway, GATEWAY_PORT), (console_proc, CONSOLE_PORT)]):
            print("❌ 服务未就绪", file=sys.stderr)
            return 1

        # 造两类真实待决策：① 待审审批（业务决策）② 被 E1 阻断的检查（条件缺口）
        approval = httpx.post(f"{gbase}/approvals", headers=hdr, timeout=30, json={
            "type": "GATE_REQUIREMENT_REVIEW", "stage": "产品域 G1", "requester": "产品Agent@wp10",
            "task_ref": "wp10-task",
            "context_snapshot": {
                "artifact_ref": "PRD v1", "diff_summary": "报表导出需求 PRD",
                "evidence": ["https://example.com/prd"],
                "sources": [{"kind": "voice", "ref": "feishu://chat/oc/msg/om_v1",
                             "quote": "按月分仓", "locator": {"start_ms": 41200}}],
                "risk": {"score": 20, "level": "L1", "factors": ["常规"]},
            },
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
        }).json()
        gate = httpx.post(f"{gbase}/eng-gates/E1/check", headers=hdr, timeout=30, json={
            "task_ref": "wp10-task",
            "context": {"prd_ref": "PRD v1", "sources": [{"kind": "chat", "ref": "feishu://x", "quote": "加导出", "confirmed_by": "运营-张"}],
                        "change_targets": ["src/report/export.py"], "rollback_plan": "切回"},
            # 缺 impact_scope（critical）→ 应被阻断
        }).json()

        queue = httpx.get(f"{cbase}/console", headers=hdr, timeout=30).json()
        counts = queue["counts"]
        check("① 队列聚合待审审批与闸门阻断", counts["approvals"] >= 1 and counts["gate_blocks"] >= 1,
              f"counts={counts}")

        apv_item = next((i for i in queue["queue"] if i["kind"] == "approval"), {})
        gate_item = next((i for i in queue["queue"] if i["kind"] == "gate_block"), {})
        check("② 每条带「为什么卡住 + 动作 + 深链」",
              bool(apv_item.get("why_waiting") and apv_item.get("actions") and apv_item.get("deep_link"))
              and bool(gate_item.get("why_waiting") and gate_item.get("actions")),
              f"审批项动作={apv_item.get('actions')}；闸门项动作={gate_item.get('actions')}")
        check("③ 队列展示来源链与风险（决策依据可见）",
              apv_item.get("source_count") == 1 and apv_item.get("risk") == "L1",
              f"sources={apv_item.get('source_count')} risk={apv_item.get('risk')}")
        check("④ 上游不可用时如实报告（运行归档故意关闭）",
              any("运行归档" in e for e in queue["upstream_errors"]),
              f"upstream_errors={queue['upstream_errors']}")

        # 决策提交（走审批网关留痕）
        decided = httpx.post(f"{cbase}/console/decide", headers=hdr, timeout=30,
                             json={"approval_id": approval["id"], "decision": "approve",
                                   "comment": "驾驶舱批准", "decided_by": "值班经理"}).json()
        check("⑤ 驾驶舱决策提交成功", decided.get("decision") == "approve",
              f"decision={decided.get('decision')} by={decided.get('decided_by')}")
        after = httpx.get(f"{gbase}/approvals/{approval['id']}", headers=hdr, timeout=30).json()
        check("⑥ 决策在审批网关留痕（未绕过审计）",
              after["status"] == "approved" and after["decisions"][-1]["decided_by"] == "值班经理",
              f"status={after['status']} decisions={len(after['decisions'])}")

        # 误拦反馈闭环（M1 决策 D3）
        stats_before = httpx.get(f"{gbase}/eng-gates/feedback/stats", headers=hdr, timeout=30).json()
        fb = httpx.post(f"{cbase}/console/feedback", headers=hdr, timeout=30,
                        json={"check_id": gate["check_id"], "verdict": "false_positive",
                              "note": "影响范围其实已在 PRD 中写明", "by": "评审员"}).json()
        stats_after = httpx.get(f"{gbase}/eng-gates/feedback/stats", headers=hdr, timeout=30).json()
        check("⑦ 误拦反馈可提交并计入统计",
              fb.get("feedback") == "false_positive"
              and stats_after["judged"] == stats_before["judged"] + 1
              and stats_after["false_positive_rate"] == 1.0,
              f"误拦率 {stats_before.get('false_positive_rate')} → {stats_after.get('false_positive_rate')}"
              f"（已反馈 {stats_after['judged']}/{stats_after['blocked_total']}）")

        html = httpx.get(f"{cbase}/", headers=hdr, timeout=30).text
        check("⑧ 面板渲染待决策队列与误拦率",
              "待决策队列" in html and "误拦率" in html and "其实可放行" in html,
              "面板含待决策区块与反馈按钮")

        ok = all(c[1] for c in checks)
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"wp10-decision-console-drill-{stamp}.md"
        rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
        report.write_text(f"""# WP10 驾驶舱演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 队列快照：`{counts}`｜上游错误：`{queue['upstream_errors']}`
- 误拦统计：`{stats_after}`
- 对应课程：图 67/68（驾驶舱显示进度与待决策事项、点击直达上下文）

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 结论

{'全部通过：待决策队列可用、决策经网关留痕、误拦反馈形成闭环（误拦率可计算）。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
        print(f"\n报告：{report.relative_to(ROOT)}")
        print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        for p in (console_proc, gateway):
            try:
                p.send_signal(signal.SIGKILL)
            except Exception:
                pass
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
