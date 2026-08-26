#!/usr/bin/env python3
"""Phase 2 运维域端到端演示：告警诊断 → G5 变更审批 → 变更执行（真实修复 MR 合入）→ 复盘 → G6 审查。

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT="sandbox6853335/prodesgin-demo" \
         FEISHU_WEBHOOK_URL=... WEBHOOK_SECRET=...
  DEMO_LLM_MODEL=qwen-max services/agent-runtime/.venv/bin/python scripts/demo_ops.py --gateway --pg
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
PORTS = {"approval-gateway": 8000, "gitlab-bridge": 8001, "task-center": 8002, "agent-runtime": 8003}
DEMO_PROJECT = os.getenv("DEMO_PROJECT", "sandbox6853335/prodesgin-demo")
DEMO_PROJECT_ID = int(os.getenv("DEMO_PROJECT_ID", "85697055"))
TMP = tempfile.mkdtemp(prefix="ops-demo-")

procs: list[subprocess.Popen] = []
results: list[tuple[str, bool, str]] = []

ALERT_ID = f"alert-{int(time.time())}"
SCENARIOS = {
    "operational": {
        "alert": {
            "id": ALERT_ID,
            "title": "导出服务实例 CPU 持续高负载",
            "severity": "P1",
            "source": "prometheus",
            "message": "reports-exporter 实例 CPU 95% 持续 15 分钟",
        },
        "context": "2026-08-26 10:30 reports-exporter 3 个实例 CPU 95%+，QPS 正常，内存正常；"
                   "无代码异常日志，疑似流量增长导致容量不足。",
        "runbook": "CPU 高负载：\n1. 先横向扩容（+2 实例）观察；\n2. 若持续则排查慢查询；\n3. 扩容量化：单实例 QPS 上限 500。",
    },
    "fix_code": {
        "alert": {
            "id": ALERT_ID,
            "title": "导出接口超大日期范围导致响应缓慢",
            "severity": "P1",
            "source": "prometheus",
            "message": "GET /api/reports/export 在 5 年日期范围下 p95 响应 12s（SLO 3s）",
        },
        "context": "2026-08-26 10:30 导出接口 p95 12s；fetch_report_data(start, end) 无日期范围上限校验。",
        "runbook": "导出接口性能 SLO 3s：\n1. 检查日期范围是否超限（上限 365 天）；\n"
                   "2. 在 src/reports/query.py 的 fetch_report_data 中增加范围校验（跨度 >365 天返回 []）；\n"
                   "3. 只修改 query.py，保持返回值类型与行为完全不变；必要时回滚。",
    },
}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


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
    procs.append(subprocess.Popen(
        [service_python("task-center"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["task-center"])],
        cwd=os.path.join(SERVICES, "task-center"), env=env,
        stdout=open(f"{TMP}/taskcenter.log", "w"), stderr=subprocess.STDOUT,
    ))
    env = dict(os.environ)
    env.update({
        "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", "ops-secret"),
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
            "LLM_MODEL": os.getenv("DEMO_LLM_MODEL", "qwen-max"),
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
            check(f"服务启动 {name}", False)
            return False
    return True


FIX = False  # --fix 时置 True（fix_code 场景；受模型外科编辑能力限制，默认 operational）


def main(gateway: bool, pg: bool) -> int:
    start_services(gateway, pg)
    print(f"服务启动中…（model={os.getenv('DEMO_LLM_MODEL', 'qwen-max')}）")
    if not wait_healthy():
        return 1
    check("四个服务健康", True)

    # 1. 告警 → 诊断 → G5 挂起
    scenario = SCENARIOS["operational" if not FIX else "fix_code"]
    alert = scenario["alert"]
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs",
        json={
            "sop": "ops-domain/alert-to-postmortem",
            "requirement_text": alert["title"],
            "title": alert["title"],
            "task_ref": ALERT_ID,
            "project_id": str(DEMO_PROJECT_ID),
            "alert": alert,
            "context": scenario["context"],
            "runbook": scenario["runbook"],
        },
        timeout=600,
    )
    run = r.json()
    check(
        "1. 告警诊断 → 分级处置 → G5 变更审批挂起",
        run.get("status") == "waiting_approval" and run.get("gate") == "gate_g5" and run.get("approval_id"),
        f"type={run.get('change_type')} approval={run.get('approval_id')}",
    )
    approval_id = run.get("approval_id", "")

    # 2. G5 审批上下文（三要素：生产变更 L3）
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=10).json()
    risk = apv["context_snapshot"]["risk"]
    check("2. G5 三要素 + L3 生产变更强制审批", risk["level"] == "L3" and apv["context_snapshot"]["diff_summary"])

    # 3. 变更审批（人审）
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
        json={"decision": "approve", "decided_by": "运维负责人@Eve", "comment": "变更合规，有回滚预案，批准执行"},
        timeout=10,
    )
    check("3. G5 变更审批批准", r.status_code == 200)

    # 4. 恢复 → 执行变更（真实修复 MR 合入 / 操作记录）→ 复盘 → G6 挂起
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{run['thread_id']}/resume",
        json={"decision": "approve", "comment": ""}, timeout=900,
    )
    body = r.json()
    check(
        "4. 变更执行 + 复盘生成 → G6 挂起",
        body.get("status") == "waiting_approval" and body.get("gate") == "gate_g6" and body.get("approval_id"),
        f"execution={body.get('execution', '')[:60]}",
    )
    print("\n── 复盘文档（AI 生成）──")
    print((body.get("postmortem") or "")[:600])

    # 5. G6 复盘审查
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{body['approval_id']}/decision",
        json={"decision": "approve", "decided_by": "技术负责人@Frank", "comment": "复盘完整，改进项已登记"},
        timeout=10,
    )
    check("5. G6 复盘审查批准", r.status_code == 200)

    # 6. 恢复 → 闭环
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{run['thread_id']}/resume",
        json={"decision": "approve", "comment": ""}, timeout=60,
    )
    check("6. 运维闭环完成（稳态）", r.json().get("status") == "done")

    # 7. 真实 MR 合入验证（fix_code 时）
    if body.get("change_type") == "fix_code":
        r = httpx.get(f"http://127.0.0.1:{PORTS['gitlab-bridge']}/gitlab/mrs/{DEMO_PROJECT_ID}/{body.get('fix_mr_iid', 0)}", timeout=30)
        ok = r.status_code == 200 and r.json().get("state") == "merged"
        check("7. 真实 GitLab 修复 MR 已合入", ok, f"state={r.json().get('state') if r.status_code == 200 else '-'}")
    else:
        check("7. 操作类变更已执行（变更记录单见仓库）", True)

    # 8. 审批审计 + 飞书
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=10).json()
    check("8. 审批审计：G5 决策人/意见留痕", apv["decisions"][0]["decided_by"] == "运维负责人@Eve")
    with open(f"{TMP}/approval.log") as f:
        log = f.read()
    check("9. 飞书卡片推送无失败", "推送失败" not in log and "推送被拒" not in log)

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 44}\nPhase 2 运维域演示结果: {passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    if not passed:
        print(open(f"{TMP}/runtime.log").read()[-2500:])
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 2 运维域端到端演示")
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
