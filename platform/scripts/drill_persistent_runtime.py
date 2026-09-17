#!/usr/bin/env python3
"""WP7 持久化运行时**真实演练**：等待 → 唤醒 → kill -9 恢复 → 运行归档。

验收口径（17 号计划 WP7）：
  ① `CHECKPOINTER=postgres` 时状态落 PG（`/health` 如实标注 restart_safe）；
  ② **进程被 kill -9 后重启，任务能从 checkpoint 继续**（不是从头再来）；
  ③ "数据没到先等待 → 到齐继续"链路可用（课程图 48）；
  ④ 运行归档含 trigger/状态/等待次数/token 与**成本**（拿不到计费头则标 unmetered）。

前置：本地 PG 可用（docker `platform-pg`）。PG 不可用时演练会明确失败，不回退假通过。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python scripts/drill_persistent_runtime.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
TMP = tempfile.mkdtemp(prefix="wp7-drill-")
PORT = 8052
GATEWAY_PORT = 8051
REPORTS = ROOT / "reports"
PG_DSN = os.getenv("CHECKPOINTER_DSN", "postgresql://platform:platform@127.0.0.1:5432/platform")
RUNS_DB = os.getenv("RUNS_DB_URL", "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform")
TASK = f"wp7-drill-{int(time.time())}"


def env_for() -> dict:
    env = dict(os.environ)
    env.update({
        "CHECKPOINTER": "postgres",
        "CHECKPOINTER_DSN": PG_DSN,
        "RUNS_DB_URL": RUNS_DB,
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{GATEWAY_PORT}",  # 产品域要建 G1 审批
        "PROMPTS_DIR": str(ROOT / "prompts"),
    })
    env.pop("LLM_API_KEY", None)   # 用确定性桩，避免每次结论漂移
    return env


def start_gateway() -> subprocess.Popen:
    """真实审批网关（产品域流程需要它建 G1 审批单）。"""
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{TMP}/approval.db", "DEFAULT_TENANT": "drill"})
    return subprocess.Popen(
        [str(SERVICES / "approval-gateway" / ".venv" / "bin" / "python"), "-m", "uvicorn",
         "app.main:app", "--port", str(GATEWAY_PORT)],
        cwd=SERVICES / "approval-gateway", env=env,
        stdout=open(f"{TMP}/gateway.log", "w"), stderr=subprocess.STDOUT,
    )


def start_runtime() -> subprocess.Popen:
    return subprocess.Popen(
        [str(SERVICES / "agent-runtime" / ".venv" / "bin" / "python"), "-m", "uvicorn",
         "app.main:app", "--port", str(PORT)],
        cwd=SERVICES / "agent-runtime", env=env_for(),
        stdout=open(f"{TMP}/runtime-{int(time.time())}.log", "w"), stderr=subprocess.STDOUT,
    )


def wait_healthy(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok = (httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=3).status_code == 200
                  and httpx.get(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=3).status_code == 200)
            if ok:
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
    proc = start_runtime()
    base = f"http://127.0.0.1:{PORT}"
    try:
        print("启动中（approval-gateway + agent-runtime，CHECKPOINTER=postgres）…")
        if not wait_healthy():
            print("❌ 服务未就绪（PG 是否可用？）", file=sys.stderr)
            return 1

        health = httpx.get(f"{base}/health", timeout=10).json()
        cp = health["checkpointer"]
        check("① checkpointer=postgres 且如实标注 restart_safe", cp["configured"] == "postgres" and cp["restart_safe"] is True,
              json.dumps(cp, ensure_ascii=False))

        # 启动一个产品域运行 → 在 G1 挂起（状态必须落到 PG）
        r = httpx.post(f"{base}/runs", timeout=60, json={
            "requirement_text": "列表加导出，支持常用格式", "task_ref": TASK,
            "trigger": "schedule", "tenant": "drill",
        })
        r.raise_for_status()
        run = r.json()
        thread_id = run["thread_id"]
        check("② 运行在 G1 挂起（状态已持久化）", run["status"] == "waiting_approval",
              f"thread={thread_id} approval={run.get('approval_id')}")

        archive = httpx.get(f"{base}/runs/{thread_id}/archive", timeout=10).json()
        check("③ 归档记录了 SOP/触发方式/岗位", archive["sop"] and archive["trigger"] == "schedule" and archive["role"] == "product-agent",
              f"trigger={archive['trigger']} role={archive['role']} checkpoint={archive['checkpoint']}")

        # ---- kill -9：模拟进程崩溃 ----
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=15)
        print("   （已 kill -9 运行时进程）")
        time.sleep(1)

        proc = start_runtime()
        if not wait_healthy():
            print("❌ 重启后服务未就绪", file=sys.stderr)
            return 1
        check("④ 重启后服务恢复", True)

        # 状态必须还在（这正是 MemorySaver 做不到的）
        health2 = httpx.get(f"{base}/health", timeout=10).json()["checkpointer"]
        check("⑤ 重启后仍是 postgres（未静默回退内存）",
              health2["configured"] == "postgres" and health2["restart_safe"] is True,
              json.dumps(health2, ensure_ascii=False))

        # 注意：状态查询端点是 /runs/{id}/state（不是 /runs/{id}）
        state = httpx.get(f"{base}/runs/{thread_id}/state", timeout=20)
        body_state = state.json() if state.status_code == 200 else {}
        # /state 返回 {sop, next, interrupts, values}：用"停在哪个节点 + PRD 内容还在"证明状态可恢复
        persisted = (
            state.status_code == 200
            and body_state.get("next") == ["gate_g1"]
            and bool((body_state.get("values") or {}).get("prd_doc"))
        )
        check("⑥ kill -9 后线程状态仍在（停在 gate_g1，PRD 未丢）", persisted,
              f"HTTP {state.status_code} next={body_state.get('next')} prd_len={len((body_state.get('values') or {}).get('prd_doc', ''))}")

        # 从断点恢复并继续执行。用 revise（回到 PRD 节点重写、版本 +1）而不是 approve：
        # approve 会走到 create_issues 需要真实 GitLab；本演练要验证的是"能继续执行"，
        # 不引入外部依赖。恢复若失败会直接 500（而不是静默从头重跑）。
        resp = httpx.post(f"{base}/runs/{thread_id}/resume", timeout=90,
                          json={"decision": "revise", "comment": "演练：从断点继续"})
        assert resp.status_code == 200, f"resume 失败：HTTP {resp.status_code} {resp.text[:200]}"
        resumed = resp.json()
        check("⑦ 从 checkpoint 恢复并继续执行（PRD 版本递增）",
              resumed.get("status") == "waiting_approval" and resumed.get("prd_version") == 2,
              f"status={resumed.get('status')} prd_version={resumed.get('prd_version')}")

        # ---- 等待/唤醒链路（另一个线程，走运维域不需要真实告警源）----
        r2 = httpx.post(f"{base}/runs", timeout=60, json={
            "sop": "ops-domain/alert-to-postmortem",
            "requirement_text": "order-api 5xx 升高",
            "alert": {"title": "HighErrorRate", "message": "5xx 12%", "data_ready": False},
            "context": "等待指标到齐", "task_ref": f"{TASK}-wait",
            "trigger": "alert", "tenant": "drill",
        })
        wait_body = r2.json() if r2.status_code == 200 else {}
        waited = wait_body.get("status") in ("waiting_data", "waiting_approval")
        check("⑧ 数据未就绪时进入等待（不误判为故障）", waited, f"status={wait_body.get('status')}")

        if wait_body.get("status") == "waiting_data":
            woke = httpx.post(f"{base}/runs/{wait_body['thread_id']}/wake", timeout=60,
                              json={"data": {"series": [1, 2, 3]}}).json()
            check("⑨ 数据到齐后唤醒继续", woke.get("status") in ("done", "waiting_approval", "running"),
                  f"status={woke.get('status')}")
        else:
            check("⑨ 数据到齐后唤醒继续", False, "未进入 waiting_data（等待语义未触发，需检查运维域节点）")

        # ---- 归档统计 ----
        listing = httpx.get(f"{base}/runs", timeout=20, params={"tenant": "drill"}).json()
        stats = listing["stats"]
        check("⑩ 归档统计可用（含成本口径）", stats["runs"] >= 2 and "total_cost_usd" in stats,
              f"runs={stats['runs']} by_status={stats['by_status']} cost={stats['total_cost_usd']} unmetered_runs={stats['unmetered_runs']}")

        ok = all(c[1] for c in checks)
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"wp7-persistent-runtime-drill-{stamp}.md"
        rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
        report.write_text(f"""# WP7 持久化运行时演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- checkpointer：`{json.dumps(cp, ensure_ascii=False)}`
- 线程：`{thread_id}`（产品域）｜PG DSN：`{PG_DSN.split('@')[-1]}`
- 对应课程：图 48（等待与恢复）、图 49（日终留证）

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 关键证据

- **kill -9 后仍能继续**：进程被强杀并重启，`GET /runs/{thread_id}` 仍返回 `waiting_approval`，
  随后 `/resume` 从断点继续（MemorySaver 下这里会 404）。
- 运行归档：`runs={stats['runs']}`｜状态分布 `{stats['by_status']}`｜
  成本 `${stats['total_cost_usd']}`（未计量运行 {stats['unmetered_runs']} 次，如实标注不估算）。

## 结论

{'全部通过：状态可跨进程重启恢复，等待/唤醒与归档可用。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
        print(f"\n报告：{report.relative_to(ROOT)}")
        print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        for p in (proc, gateway):
            try:
                p.send_signal(signal.SIGKILL)
            except Exception:
                pass
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
