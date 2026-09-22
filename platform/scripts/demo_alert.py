#!/usr/bin/env python3
"""Phase 2 收尾①演示：Prometheus 告警真实接入 → 自动触发运维域 SOP（Runbook RAG 诊断）。

链路：
  Alertmanager webhook → task-center（建任务 + 自动触发）
    → agent-runtime 运维域 SOP（diagnose：知识库检索 Runbook，带来源引用）
    → G5 变更审批（飞书卡片）→ 执行 → AI 复盘 → G6 审查 → 闭环

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT=... FEISHU_WEBHOOK_URL=... WEBHOOK_SECRET=...
  DEMO_LLM_MODEL=qwen-max services/agent-runtime/.venv/bin/python scripts/demo_alert.py --gateway --pg
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
PORTS = {
    "approval-gateway": 8000,
    "gitlab-bridge": 8001,
    "task-center": 8002,
    "agent-runtime": 8003,
    "knowledge-base": 8004,
}
DEMO_PROJECT = os.getenv("DEMO_PROJECT", "sandbox6853335/prodesgin-demo")
DEMO_PROJECT_ID = os.getenv("DEMO_PROJECT_ID", "85697055")
TMP = tempfile.mkdtemp(prefix="alert-demo-")

procs: list[subprocess.Popen] = []
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def service_python(name: str) -> str:
    venv = os.path.join(SERVICES, name, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def start_services(gateway: bool, pg: bool) -> None:
    db = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform" if pg else None

    env = dict(os.environ)
    env["DATABASE_URL"] = db or f"sqlite:///{TMP}/approval.db"
    env["PUBLISH_TARGETS"] = f"http://127.0.0.1:{PORTS['task-center']}/events"
    env["FEISHU_WEBHOOK_URL"] = os.getenv("FEISHU_WEBHOOK_URL", "")
    env["APPROVAL_WEB_BASE_URL"] = f"http://127.0.0.1:{PORTS['approval-gateway']}"
    procs.append(subprocess.Popen(
        [service_python("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["approval-gateway"])],
        cwd=os.path.join(SERVICES, "approval-gateway"), env=env,
        stdout=open(f"{TMP}/approval.log", "w"), stderr=subprocess.STDOUT,
    ))

    env = dict(os.environ)
    env["DATABASE_URL"] = db or f"sqlite:///{TMP}/taskcenter.db"
    env["AGENT_RUNTIME_URL"] = f"http://127.0.0.1:{PORTS['agent-runtime']}"
    env["ALERT_WEBHOOK_SECRET"] = os.getenv("WEBHOOK_SECRET", "alert-secret")
    env["OPS_PROJECT_ID"] = DEMO_PROJECT_ID
    procs.append(subprocess.Popen(
        [service_python("task-center"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["task-center"])],
        cwd=os.path.join(SERVICES, "task-center"), env=env,
        stdout=open(f"{TMP}/taskcenter.log", "w"), stderr=subprocess.STDOUT,
    ))

    env = dict(os.environ)
    env.update({
        "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", "alert-secret"),
        "GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
        "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "PUBLISH_TARGETS": f"http://127.0.0.1:{PORTS['task-center']}/events",
    })
    procs.append(subprocess.Popen(
        [service_python("gitlab-bridge"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["gitlab-bridge"])],
        cwd=os.path.join(SERVICES, "gitlab-bridge"), env=env,
        stdout=open(f"{TMP}/bridge.log", "w"), stderr=subprocess.STDOUT,
    ))

    # 知识库：Qwen 向量（Runbook RAG）
    env = dict(os.environ)
    env.update({
        "LLM_API_KEY": "sk-litellm-master",
        "LLM_BASE_URL": "http://127.0.0.1:4000/v1",
        "EMBEDDING_MODEL": "qwen-embed",
        "EMBEDDING_DIM": "1024",
        "QDRANT_URL": "http://localhost:6333",
        "KB_COLLECTION": "runbooks_kb",
    })
    procs.append(subprocess.Popen(
        [service_python("knowledge-base"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["knowledge-base"])],
        cwd=os.path.join(SERVICES, "knowledge-base"), env=env,
        stdout=open(f"{TMP}/kb.log", "w"), stderr=subprocess.STDOUT,
    ))

    env = dict(os.environ)
    env.update({
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['gitlab-bridge']}",
        "KNOWLEDGE_BASE_URL": f"http://127.0.0.1:{PORTS['knowledge-base']}",
        "PUBLISH_TARGETS": f"http://127.0.0.1:{PORTS['task-center']}/events",
        "GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
        "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "DEMO_PROJECT": DEMO_PROJECT,
    })
    if gateway:
        env.update({
            "LLM_API_KEY": "sk-litellm-master",
            "LLM_BASE_URL": "http://127.0.0.1:4000/v1",
            "LLM_MODEL": os.getenv("DEMO_LLM_MODEL", "qwen-max"),
        })
    procs.append(subprocess.Popen(
        [service_python("agent-runtime"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["agent-runtime"])],
        cwd=os.path.join(SERVICES, "agent-runtime"), env=env,
        stdout=open(f"{TMP}/runtime.log", "w"), stderr=subprocess.STDOUT,
    ))


def wait_healthy() -> bool:
    deadline = time.time() + 90
    for name, port in PORTS.items():
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            check(f"服务启动 {name}", False)
            return False
    return True


def seed_runbooks() -> int:
    import glob

    files = sorted(glob.glob(os.path.join(ROOT, "runbooks", "*.md")))
    ok = 0
    for path in files:
        source = os.path.basename(path)
        content = open(path, encoding="utf-8").read()
        r = httpx.post(f"http://127.0.0.1:{PORTS['knowledge-base']}/documents",
                       json={"source": source, "content": content}, timeout=180)
        if r.status_code == 200:
            ok += 1
    return ok


def main(gateway: bool, pg: bool) -> int:
    start_services(gateway, pg)
    print(f"服务启动中…（model={os.getenv('DEMO_LLM_MODEL', 'qwen-max')}）")
    if not wait_healthy():
        return 1
    check("五个服务健康（含知识库）", True)

    chunks = seed_runbooks()
    check("1. Runbook 入库知识库（Qwen 向量）", chunks >= 3, f"{chunks} 篇")

    hits = httpx.post(f"http://127.0.0.1:{PORTS['knowledge-base']}/search",
                      json={"query": "导出接口 p95 超过 SLO 3s 怎么处理", "limit": 3}, timeout=60).json()
    check("2. Runbook 检索可用（带来源）",
          bool(hits) and all(h.get("source") for h in hits),
          f"top={hits[0]['source'] if hits else '-'} score={round(hits[0]['score'], 3) if hits else '-'}")

    # 3. 真实 Alertmanager webhook → 任务 + 自动触发运维域 SOP
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['task-center']}/alerts/prometheus",
        # 鉴权专项：告警 webhook 现在要求共享密钥（此前无鉴权，任何人可凭空拉运维 SOP）
        headers={"X-Webhook-Token": os.getenv("WEBHOOK_SECRET", "alert-secret")},
        json={
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "ExportHighLatency",
                        "severity": "P1",
                        "job": "reports-exporter",
                        "fingerprint": f"fp{int(time.time())}",
                    },
                    "annotations": {
                        "summary": "导出接口 p95 延迟 12s（SLO 3s）",
                        "description": "reports-exporter 导出接口 p95 12s，日期跨度 5 年，疑似缺失范围校验",
                    },
                }
            ],
        },
        timeout=30,
    )
    check("3. Prometheus 告警接入 → 建任务", r.status_code == 200 and len(r.json().get("tasks", [])) == 1)

    # 4. 等待自动触发的运维域 run 挂起在 G5（Runbook RAG 诊断）
    deadline = time.time() + 300
    run = None
    thread_id = None
    while time.time() < deadline:
        tasks = httpx.get(f"http://127.0.0.1:{PORTS['task-center']}/tasks", timeout=10).json()
        task = next((t for t in tasks if t["sop"] == "ops-domain/alert-to-postmortem"), None)
        if task:
            thread_id = task["external_ref"].replace("#", "-")
            try:
                snap = httpx.get(f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/state", timeout=10).json()
            except httpx.HTTPError:
                snap = {}
            if snap.get("next") and "gate_g5" in snap["next"]:
                run = snap
                break
        time.sleep(5)

    check("4. 告警自动触发运维域 SOP → G5 挂起",
          run is not None and bool(run.get("values", {}).get("approval_id_g5")),
          f"thread={thread_id}")
    if run is None:
        print(open(f"{TMP}/taskcenter.log").read()[-1500:])
        print(open(f"{TMP}/runtime.log").read()[-1500:])
        return 1

    values = run["values"]
    sources = values.get("runbook_sources", [])
    check("5. 诊断使用知识库 Runbook（引用留痕）", bool(sources), f"sources={sources}")
    print("\n── 诊断结论（基于 Runbook RAG）──")
    print((values.get("diagnosis") or "")[:400])

    # 5. G5 审批 → 执行 → 复盘 → G6
    apv_id = values["approval_id_g5"]
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{apv_id}", timeout=10).json()
    check("6. G5 审批依据含 Runbook 来源",
          any("Runbook 来源" in e for e in apv["context_snapshot"]["evidence"]),
          f"type={values.get('change_type')}")

    httpx.post(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{apv_id}/decision",
               json={"decision": "approve", "decided_by": "运维负责人@Eve", "comment": "按 Runbook 处置，批准"}, timeout=10)
    r = httpx.post(f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/resume",
                   json={"decision": "approve", "comment": ""}, timeout=900)
    body = r.json()
    check("7. 变更执行 + 复盘 → G6 挂起",
          body.get("status") == "waiting_approval" and body.get("gate") == "gate_g6",
          f"execution={body.get('execution', '')[:50]}")

    httpx.post(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{body['approval_id']}/decision",
               json={"decision": "approve", "decided_by": "技术负责人@Frank", "comment": "复盘通过"}, timeout=10)
    r = httpx.post(f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/resume",
                   json={"decision": "approve", "comment": ""}, timeout=60)
    check("8. 运维闭环完成", r.json().get("status") == "done")

    with open(f"{TMP}/approval.log") as f:
        log = f.read()
    check("9. 飞书卡片推送无失败", "推送失败" not in log and "推送被拒" not in log)

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 48}\n告警接入演示结果: {passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 2 告警接入 + Runbook RAG 演示")
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
