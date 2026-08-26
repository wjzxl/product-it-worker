#!/usr/bin/env python3
"""Phase 2 测试域批量试点：对已合入的试点功能批量跑测试域 SOP（独立用例→执行→缺陷闭环→G4），
统计：用例生成成功率 / 缺陷发现率 / 缺陷闭环率 / G4 决策耗时 / 端到端耗时。

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT="sandbox6853335/prodesgin-demo" \
         FEISHU_WEBHOOK_URL=... WEBHOOK_SECRET=...
  services/agent-runtime/.venv/bin/python scripts/batch_test_pilot.py --gateway --pg
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
REPORTS = os.path.join(ROOT, "reports")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from batch_pilot import FEATURES  # noqa: E402  复用研发域试点的功能与 PRD

PORTS = {"approval-gateway": 8000, "gitlab-bridge": 8001, "task-center": 8002, "agent-runtime": 8003}
DEMO_PROJECT = os.getenv("DEMO_PROJECT", "sandbox6853335/prodesgin-demo")
DEMO_PROJECT_ID = int(os.getenv("DEMO_PROJECT_ID", "85697055"))
TMP = tempfile.mkdtemp(prefix="testpilot-")

procs: list[subprocess.Popen] = []


def service_python(name: str) -> str:
    venv = os.path.join(SERVICES, name, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def start_services(gateway: bool, pg: bool) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform" if pg else f"sqlite:///{TMP}/approval.db"
    env["PUBLISH_TARGETS"] = f"http://127.0.0.1:{PORTS['task-center']}/events"
    env["FEISHU_WEBHOOK_URL"] = os.getenv("FEISHU_WEBHOOK_URL", "")
    env["APPROVAL_WEB_BASE_URL"] = f"http://127.0.0.1:{PORTS['approval-gateway']}"
    procs.append(subprocess.Popen(
        [service_python("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["approval-gateway"])],
        cwd=os.path.join(SERVICES, "approval-gateway"), env=env,
        stdout=open(f"{TMP}/approval.log", "w"), stderr=subprocess.STDOUT,
    ))
    env = dict(os.environ)
    env["DATABASE_URL"] = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform" if pg else f"sqlite:///{TMP}/taskcenter.db"
    env["AGENT_RUNTIME_URL"] = f"http://127.0.0.1:{PORTS['agent-runtime']}"
    procs.append(subprocess.Popen(
        [service_python("task-center"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["task-center"])],
        cwd=os.path.join(SERVICES, "task-center"), env=env,
        stdout=open(f"{TMP}/taskcenter.log", "w"), stderr=subprocess.STDOUT,
    ))
    env = dict(os.environ)
    env.update({
        "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", "testpilot-secret"),
        "GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
        "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "PUBLISH_TARGETS": f"http://127.0.0.1:{PORTS['task-center']}/events",
    })
    procs.append(subprocess.Popen(
        [service_python("gitlab-bridge"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["gitlab-bridge"])],
        cwd=os.path.join(SERVICES, "gitlab-bridge"), env=env,
        stdout=open(f"{TMP}/bridge.log", "w"), stderr=subprocess.STDOUT,
    ))
    env = dict(os.environ)
    env.update({
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['gitlab-bridge']}",
        "PUBLISH_TARGETS": f"http://127.0.0.1:{PORTS['task-center']}/events",
        "GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
        "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "DEMO_PROJECT": DEMO_PROJECT,
    })
    if gateway:
        env.update({
            "LLM_API_KEY": "sk-litellm-master",
            "LLM_BASE_URL": f"http://127.0.0.1:4000/v1",
            "LLM_MODEL": os.getenv("DEMO_LLM_MODEL", "deepseek-chat"),
        })
    procs.append(subprocess.Popen(
        [service_python("agent-runtime"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["agent-runtime"])],
        cwd=os.path.join(SERVICES, "agent-runtime"), env=env,
        stdout=open(f"{TMP}/runtime.log", "w"), stderr=subprocess.STDOUT,
    ))


def wait_healthy() -> bool:
    deadline = time.time() + 60
    for name, port in PORTS.items():
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print(f"❌ 服务 {name} 启动超时")
            return False
    return True


def poll_g4(thread_id: str, timeout: float) -> dict:
    """轮询直到进入 gate_g4；超时返回 timeout。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/state", timeout=15)
            snap = r.json()
        except httpx.HTTPError:
            time.sleep(5)
            continue
        values = snap.get("values", {})
        if snap.get("next"):
            if "gate_g4" in snap["next"]:
                return {"status": "waiting_g4", "values": values, "snap": snap}
        else:
            return {"status": "ended", "values": values}
        time.sleep(5)
    return {"status": "timeout", "values": {}}


def decide(approval_id: str, decision: str, by: str) -> float:
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=10).json()
    created = datetime.fromisoformat(apv["created_at"].replace("Z", "+00:00"))
    httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
        json={"decision": decision, "decided_by": by, "comment": "测试域批量试点自动审批"},
        timeout=10,
    )
    return (datetime.now(timezone.utc) - created).total_seconds()


def run_feature(idx: int, feat: dict) -> dict:
    title = feat["title"]
    t0 = time.time()
    ref = f"test-{idx}-{int(time.time())}"
    thread_id = ref.replace("#", "-")

    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs",
        json={
            "sop": "test-domain/release-gate",
            "requirement_text": title,
            "title": title,
            "task_ref": ref,
            "project_id": str(DEMO_PROJECT_ID),
            "prd_doc": feat["prd"],
        },
        timeout=120,
    )
    if r.status_code != 200:
        return {"title": title, "status": f"启动失败:HTTP{r.status_code}", "lead_time_s": round(time.time() - t0, 1)}

    snap = poll_g4(thread_id, timeout=1200)
    if snap["status"] != "waiting_g4":
        return {"title": title, "status": f"未达G4:{snap['status']}", "lead_time_s": round(time.time() - t0, 1)}
    values = snap["values"]

    g4_lat = decide(values.get("approval_id_g4", ""), "approve", "测试域批量审批员")
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/resume",
        json={"decision": "approve", "comment": ""}, timeout=60,
    )
    done = r.json()

    defects = values.get("defects", [])
    closed = sum(1 for d in defects if d.get("closed"))
    gen_failed = "生成失败" in values.get("test_result", "")
    return {
        "title": title,
        "status": "passed" if done.get("status") == "done" else f"异常:{done.get('status')}",
        "gen_failed": gen_failed,
        "test_result": values.get("test_result", ""),
        "defects": len(defects),
        "defects_closed": closed,
        "g4_latency_s": round(g4_lat, 1),
        "lead_time_s": round(time.time() - t0, 1),
    }


def main(gateway: bool, pg: bool) -> int:
    os.makedirs(REPORTS, exist_ok=True)
    start_services(gateway, pg)
    print(f"服务启动中…（features={len(FEATURES)}）")
    if not wait_healthy():
        return 1

    rows = []
    for idx, feat in enumerate(FEATURES, 1):
        print(f"\n── 测试试点 {idx}/{len(FEATURES)}: {feat['title']} ──", flush=True)
        try:
            row = run_feature(idx, feat)
        except Exception as exc:  # 单条失败不中断批次
            row = {"title": feat["title"], "status": f"异常:{type(exc).__name__}", "lead_time_s": 0}
            print(f"  ❌ {exc}")
        rows.append(row)
        print(f"  {row}", flush=True)

    total = len(rows)
    passed = [r for r in rows if r["status"] == "passed"]
    gen_failed = [r for r in rows if r.get("gen_failed")]
    with_defects = [r for r in passed if r.get("defects", 0) > 0]
    closed_total = sum(r.get("defects_closed", 0) for r in rows)
    defects_total = sum(r.get("defects", 0) for r in rows)
    g4_lats = [r["g4_latency_s"] for r in passed if "g4_latency_s" in r]
    leads = [r["lead_time_s"] for r in rows]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": DEMO_PROJECT,
        "features": total,
        "passed_to_g4": len(passed),
        "gen_success_rate": f"{len(passed) - len(gen_failed)}/{total}",
        "gen_failed": len(gen_failed),
        "defects_found": defects_total,
        "defect_close_rate": f"{closed_total}/{defects_total}" if defects_total else "-",
        "avg_g4_decision_s": round(sum(g4_lats) / len(g4_lats), 1) if g4_lats else None,
        "avg_lead_time_s": round(sum(leads) / len(leads), 1) if leads else None,
        "rows": rows,
    }

    md = [
        f"# Phase 2 测试域批量试点报告（{report['generated_at'][:10]}）",
        "",
        f"- 项目：{DEMO_PROJECT}｜功能数：{report['features']}",
        f"- 到达 G4：{len(passed)}/{total}｜用例生成成功率：{report['gen_success_rate']}（生成失败 {report['gen_failed']}）",
        f"- 缺陷发现：{report['defects_found']} 个｜缺陷闭环率：{report['defect_close_rate']}",
        f"- 平均 G4 决策：{report['avg_g4_decision_s']}s｜平均端到端：{report['avg_lead_time_s']}s",
        "",
        "| 功能 | 状态 | 生成失败 | 测试结果 | 缺陷 | 闭环 | G4 决策(s) | 耗时(s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['title']} | {r['status']} | {'是' if r.get('gen_failed') else ''} | {r.get('test_result', '-')[:40]} | "
            f"{r.get('defects', '-')} | {r.get('defects_closed', '-')} | {r.get('g4_latency_s', '-')} | {r.get('lead_time_s', '-')} |"
        )
    report_path = os.path.join(REPORTS, f"phase2-test-pilot-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print(f"\n{'=' * 60}\n测试域试点报告已写入 {report_path}")
    print(
        f"到达 G4: {len(passed)}/{total} | 生成成功率: {report['gen_success_rate']} | "
        f"缺陷发现: {defects_total} | 闭环率: {report['defect_close_rate']} | "
        f"G4 决策: {report['avg_g4_decision_s']}s | 端到端: {report['avg_lead_time_s']}s"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 2 测试域批量试点")
    parser.add_argument("--gateway", action="store_true")
    parser.add_argument("--pg", action="store_true")
    args = parser.parse_args()
    code = 1
    try:
        code = main(gateway=args.gateway, pg=args.pg)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)
