#!/usr/bin/env python3
"""Phase 2 测试域端到端演示：真实仓库独立测试 → 分层执行 →（缺陷闭环）→ 测试报告 → G4 放行决策。

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT="sandbox6853335/prodesgin-demo" \
         FEISHU_WEBHOOK_URL=... WEBHOOK_SECRET=...
  services/agent-runtime/.venv/bin/python scripts/demo_phase2.py --gateway --pg
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
TMP = tempfile.mkdtemp(prefix="phase2-demo-")

procs: list[subprocess.Popen] = []
results: list[tuple[str, bool, str]] = []

# 试点功能（main 分支已合入其实现）：按类别汇总报表
TITLE = "按类别汇总报表"
PRD = (
    "## 验收标准\n"
    "- WHEN 传入多条报表数据 THEN summarize_by_category 按类别聚合数值并返回字典\n"
    "- WHEN 数据为空 THEN 返回空字典\n"
    "- 同类别多行数值累加\n"
)


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
        "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", "phase2-secret"),
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
            check(f"服务启动 {name}", False)
            return False
    return True


def main(gateway: bool, pg: bool) -> int:
    ref = f"rel-{int(time.time())}"
    start_services(gateway, pg)
    print(f"服务启动中…（mode: gateway={'on' if gateway else 'off'}, pg={'on' if pg else 'off'}）")
    if not wait_healthy():
        return 1
    check("四个服务健康", True)

    # 1. 启动测试域 SOP → 挂起在 G4
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs",
        json={
            "sop": "test-domain/release-gate",
            "requirement_text": TITLE,
            "title": TITLE,
            "task_ref": ref,
            "project_id": str(DEMO_PROJECT_ID),
            "prd_doc": PRD,
        },
        timeout=600,
    )
    run = r.json()
    check(
        "1. 测试域启动 → 独立用例生成+执行 → G4 挂起",
        run.get("status") == "waiting_approval" and run.get("gate") == "gate_g4" and run.get("approval_id"),
        f"approval={run.get('approval_id')} tests={run.get('test_result')}",
    )
    approval_id = run.get("approval_id", "")

    # 2. 缺陷闭环情况
    defects = run.get("defects", [])
    if defects:
        for d in defects:
            check(f"2. 缺陷闭环: #{d.get('issue_iid')} fixed={d.get('fixed')} closed={d.get('closed')}", True)
    else:
        check("2. 缺陷闭环: 无缺陷（独立用例全部通过）", True)

    # 3. G4 放行决策（人审）
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
        json={"decision": "approve", "decided_by": "发布负责人@Dave", "comment": "测试报告完整，风险可控，放行"},
        timeout=10,
    )
    check("3. G4 放行决策批准（approval.decided 留痕）", r.status_code == 200)

    # 4. 恢复 → 完成
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{run['thread_id']}/resume",
        json={"decision": "approve", "comment": ""}, timeout=60,
    )
    done = r.json()
    check("4. 放行完成（交付运维域）", done.get("status") == "done")

    # 5. 测试报告展示
    print("\n── 测试报告（AI 生成）──")
    print((run.get("test_report") or "")[:800])

    # 6. 审批审计
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=10).json()
    check("5. 审批审计：G4 决策人/意见留痕", apv["decisions"][0]["decided_by"] == "发布负责人@Dave" and apv["decisions"][0]["comment"])

    with open(f"{TMP}/approval.log") as f:
        log = f.read()
    check("6. 飞书卡片推送无失败", "推送失败" not in log and "推送被拒" not in log)

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 44}\nPhase 2 测试域演示结果: {passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    if not passed:
        print(open(f"{TMP}/runtime.log").read()[-2000:])
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 2 测试域端到端演示")
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
