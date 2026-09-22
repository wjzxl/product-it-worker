#!/usr/bin/env python3
"""WP2 工程闸门 E1 **真实拦截演练**：证明"闸门会真的阻止推进"，不是事后提示。

验收口径（17-FOD 计划 WP2）：
  ① 多数项通过但 critical 的 impact_scope 缺失 → **仍然拒绝**；
  ② warn 模式只记录不阻断（灰度）；
  ③ 条件补齐后放行；
  ④ 每次判定都有留痕（含规则版本/指纹）；
  ⑤ 拦截时**不调用 LLM**（在生成代码之前就停住）。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python scripts/drill_eng_gate_e1.py
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
TMP = tempfile.mkdtemp(prefix="wp2-drill-")
PORT = 8051
REPORTS = ROOT / "reports"

PRD = "PRD v1：列表页支持导出（含权限与口径）"
VOICE_SOURCE = {
    "kind": "voice",
    "ref": "feishu://chat/oc_drill/msg/om_voice_214",
    "quote": "我们对账是按月分仓看的，不是按天。",
    "locator": {"start_ms": 41200, "end_ms": 47000},
    "confirmed_by": "李经理",
}


def python_of(service: str) -> str:
    return str(SERVICES / service / ".venv" / "bin" / "python")


def start_gateway(port: int = PORT, extra_env: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{TMP}/gateway.db", "DEFAULT_TENANT": "drill"})
    env.update(extra_env or {})
    return subprocess.Popen(
        [python_of("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=SERVICES / "approval-gateway", env=env,
        stdout=open(f"{TMP}/gateway.log", "w"), stderr=subprocess.STDOUT,
    )


def wait_healthy(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=3).status_code == 200:
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

    proc = start_gateway()
    base = f"http://127.0.0.1:{PORT}"
    try:
        print("approval-gateway 启动中…")
        if not wait_healthy():
            print("❌ 网关未就绪", file=sys.stderr)
            return 1
        check("闸门服务健康", True)

        # 场景 1：4/5 通过，critical 的 impact_scope 缺失
        partial = {
            "prd_ref": PRD,
            "sources": [VOICE_SOURCE],           # 业务方载体 + confirmed_by（A1）
            "change_targets": ["src/report/export.py"],
            "rollback_plan": "保留上一版本，按灰度指标验证后一键切回",
            # impact_scope 故意缺失（critical）
        }
        r = httpx.post(f"{base}/eng-gates/E1/check", json={"task_ref": "wp2-drill-1", "context": partial},
                       headers={"X-Tenant": "drill"}, timeout=30).json()
        check("① 多数通过但 critical 缺失 → 拒绝", r["passed"] is False and r["blocked"] is True,
              f"passed={r['passed']} blocked={r['blocked']} critical_missing={r['critical_missing']}")
        passed_ids = [x["id"] for x in r["results"] if x["passed"]]
        check("② 其余 5 项确实通过（不是全盘拒绝）", len(passed_ids) == 5, f"通过项：{passed_ids}")
        gaps = {g["id"]: g for g in r["gaps"]}
        check("③ 缺口给出可执行建议", "impact_scope" in gaps and bool(gaps["impact_scope"].get("hint")),
              f"hint={gaps.get('impact_scope', {}).get('hint', '')[:40]}")
        check("④ 判定留痕含规则指纹", bool(r.get("check_id")) and bool(r.get("fingerprint")),
              f"check_id={r.get('check_id')} fingerprint={r.get('fingerprint')}")

        # 场景 2：客户端**不能**把闸门调成 warn（BUG-003/014：模式由服务端权威决定）
        override = httpx.post(f"{base}/eng-gates/E1/check",
                              json={"task_ref": "wp2-drill-2", "context": partial,
                                    "mode": "warn", "persist": False},
                              headers={"X-Tenant": "drill"}, timeout=30).json()
        check("⑤ 客户端不能把闸门降级为 warn（mode 覆盖被忽略）",
              override["mode"] == "block" and override["blocked"] is True and bool(override.get("check_id")),
              f"mode={override['mode']} blocked={override['blocked']} check_id={override.get('check_id')}")

        # 场景 2b：真正的灰度是**服务端配置**（ENG_GATES_MODE=warn）→ 只记录不阻断
        warn_proc = start_gateway(port=PORT + 1, extra_env={"ENG_GATES_MODE": "warn"})
        try:
            warn_base = f"http://127.0.0.1:{PORT + 1}"
            for _ in range(40):
                try:
                    if httpx.get(f"{warn_base}/health", timeout=3).status_code == 200:
                        break
                except Exception:
                    time.sleep(1.5)
            warn = httpx.post(f"{warn_base}/eng-gates/E1/check",
                              json={"task_ref": "wp2-drill-2b", "context": partial},
                              headers={"X-Tenant": "drill"}, timeout=30).json()
            check("⑤b 服务端 ENG_GATES_MODE=warn 时只记录不阻断",
                  warn["mode"] == "warn" and warn["blocked"] is False and warn["passed"] is False
                  and bool(warn.get("check_id")),
                  f"mode={warn['mode']} blocked={warn['blocked']} check_id={warn.get('check_id')}")
        finally:
            warn_proc.terminate()

        # 场景 3：补齐 impact_scope → 放行
        full = {**partial, "impact_scope": "复用现有 /api/v1/report/export，不改表结构，不影响其它调用方"}
        ok_run = httpx.post(f"{base}/eng-gates/E1/check", json={"task_ref": "wp2-drill-3", "context": full},
                            headers={"X-Tenant": "drill"}, timeout=30).json()
        check("⑥ 补齐影响范围后放行", ok_run["passed"] is True and ok_run["blocked"] is False,
              f"passed={ok_run['passed']} 检查项 {len(ok_run['results'])} 项全过")

        # 场景 4：来源链从审批记录自动补齐（调用方不必记得传）
        payload = {
            "type": "GATE_REQUIREMENT_REVIEW", "stage": "产品域 G1", "requester": "产品Agent@wp2-drill-4",
            "task_ref": "wp2-drill-4",
            "context_snapshot": {
                "artifact_ref": PRD, "diff_summary": "导出需求 PRD", "evidence": ["https://example.com/prd"],
                "sources": [VOICE_SOURCE], "risk": {"score": 20, "level": "L1", "factors": ["常规"]},
            },
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
        }
        httpx.post(f"{base}/approvals", json=payload, headers={"X-Tenant": "drill"}, timeout=30)
        auto = httpx.post(f"{base}/eng-gates/E1/check", json={"task_ref": "wp2-drill-4", "context": {}},
                          headers={"X-Tenant": "drill"}, timeout=30).json()
        gap_ids = [g["id"] for g in auto["gaps"]]
        check("⑦ 上下文自动补齐（来源/PRD 取自审批）", "business_source" not in gap_ids and "requirement_basis" not in gap_ids,
              f"剩余缺口：{gap_ids}")

        # 场景 5：历史留痕（度量原始数据）
        hist = httpx.get(f"{base}/eng-gates/checks", headers={"X-Tenant": "drill"}, timeout=30).json()
        blocked_n = sum(1 for h in hist if h["blocked"])
        check("⑧ 检查历史可查（拦截率可度量）", len(hist) >= 4 and blocked_n >= 1,
              f"{len(hist)} 次检查，其中阻断 {blocked_n} 次")

        # 场景 6：CLI 自检（CI 门禁）
        cli = subprocess.run([python_of("approval-gateway"), str(ROOT / "scripts" / "check_gates.py"), "--selftest"],
                             capture_output=True, text=True, cwd=ROOT)
        check("⑨ CLI 自检通过（可进 CI）", cli.returncode == 0, cli.stdout.strip().splitlines()[-1] if cli.stdout else "")

        ok = all(c[1] for c in checks)
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"wp2-eng-gate-e1-drill-{stamp}.md"
        rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
        report.write_text(f"""# WP2 工程闸门 E1 拦截演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 规则：`platform/engineering-gates/E1-pre-implementation.yaml`（指纹 {r.get('fingerprint')}，版本 {r.get('rule_version')}）
- 关键语义：**关键未知不能被多数通过抵消**（课程图 40）

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 拦截时的缺口清单（原文）

```
{r.get('check_id')} · passed={r['passed']} blocked={r['blocked']}
{chr(10).join('  - ' + g['ask'] + ' → ' + g['detail'] for g in r['gaps'])}
```

## 结论

{'全部通过：E1 在关键条件缺失时确实阻断推进，且灰度与留痕可用。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
        print(f"\n报告：{report.relative_to(ROOT)}")
        print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
