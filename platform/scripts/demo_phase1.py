#!/usr/bin/env python3
"""Phase 1 端到端演示：研发域闭环（真实 GitLab + 真实 DeepSeek 编码）。

链路：
  Issue webhook(模拟) → gitlab-bridge → task-center（建任务）
  → agent-runtime 研发域 SOP（DeepSeek 技术方案 → G2 人审 → AI 编码+质量门禁 →
    推送分支+建 MR → G3 人审 → 合入）
  审批卡片实时推送飞书（若配置 FEISHU_WEBHOOK_URL）

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT="sandbox6853335/prodesgin-demo" \
         FEISHU_WEBHOOK_URL=... WEBHOOK_SECRET=...
  services/agent-runtime/.venv/bin/python scripts/demo_phase1.py --gateway --pg
"""
from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")

PORTS = {"approval-gateway": 8000, "gitlab-bridge": 8001, "task-center": 8002, "agent-runtime": 8003}
TMP = tempfile.mkdtemp(prefix="phase1-demo-")
DEMO_PROJECT = os.getenv("DEMO_PROJECT", "sandbox6853335/prodesgin-demo")
DEMO_PROJECT_ID = int(os.getenv("DEMO_PROJECT_ID", "85697055"))  # 真实项目数字 id（任务引用键）
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "phase1-secret")

procs: list[subprocess.Popen] = []
results: list[tuple[str, bool, str]] = []


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
        "WEBHOOK_SECRET": WEBHOOK_SECRET,
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


def dump_log(name: str) -> str:
    path = f"{TMP}/{name}.log"
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()[-1500:]


def main(gateway: bool, pg: bool) -> int:
    iid = random.randint(2000, 9999)
    ref = f"{DEMO_PROJECT_ID}#{iid}"
    start_services(gateway, pg)
    print(f"服务启动中…（mode: gateway={'on' if gateway else 'off'}, pg={'on' if pg else 'off'}, iid={iid}）")
    if not wait_healthy():
        return 1
    check("四个服务健康", True)

    # 1. 模拟 Issue webhook → 任务
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['gitlab-bridge']}/webhooks/gitlab",
        headers={"X-Gitlab-Token": WEBHOOK_SECRET, "X-Gitlab-Event-UUID": f"p1-{iid}"},
        json={
            "object_kind": "issue",
            "project": {"id": DEMO_PROJECT_ID, "path_with_namespace": DEMO_PROJECT},
            "object_attributes": {
                "iid": iid, "title": "报表导出功能", "description": "支持按日期筛选导出 CSV",
                "state": "opened", "action": "open", "labels": [{"title": "product"}],
            },
        },
        timeout=10,
    )
    check("1. Issue webhook 入站", r.status_code == 200)
    time.sleep(1)

    tasks = httpx.get(f"http://127.0.0.1:{PORTS['task-center']}/tasks", timeout=10).json()
    task = next((t for t in tasks if t["external_ref"] == ref), None)
    check(f"2. 任务中心建任务 {ref}", task is not None)
    if task is None:
        print(dump_log("bridge"))
        return 1

    httpx.post(
        f"http://127.0.0.1:{PORTS['task-center']}/tasks/{task['id']}/transition",
        json={"to": "running", "by": "研发Agent", "detail": {"step": "tech_design"}},
        timeout=10,
    )

    # 2. 启动研发域 SOP → 挂起在 G2
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs",
        json={
            "sop": "dev-domain/issue-to-merge",
            "requirement_text": "报表导出功能：按日期筛选导出 CSV",
            "title": "报表导出功能",
            "task_ref": ref,
            "project_id": str(DEMO_PROJECT_ID),  # 数字项目 id：路径型端点（get/merge MR）要求单段参数
            "prd_doc": "## 验收标准\n- WHEN 用户按日期筛选 THEN 导出 CSV 含 UTF-8 BOM\n- WHEN 日期范围无数据 THEN 仍输出表头\n- 文件命名 报表_{start}_{end}.csv，单次上限 10000 行",
        },
        timeout=120,
    )
    run = r.json()
    check(
        "2. 研发域启动 → 技术方案 → G2 挂起",
        run["status"] == "waiting_approval" and run["gate"] == "gate_g2" and run["approval_id"],
        f"approval={run.get('approval_id')}",
    )
    approval_id = run["approval_id"]

    # 3. G2 人审批准（Web 审批台/飞书卡片 → 审批网关）
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
        json={"decision": "approve", "decided_by": "技术负责人@Bob", "comment": "方案可行，按此实现"},
        timeout=10,
    )
    check("3. G2 方案评审批准", r.status_code == 200)
    time.sleep(1)

    # 4. 恢复 → AI 编码（真实 DeepSeek）+ 质量门禁 + 推送分支 + 建 MR → G3 挂起
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{run['thread_id']}/resume",
        json={"decision": "approve", "comment": ""},
        timeout=600,
    )
    body = r.json()
    check(
        "4. AI 编码+测试通过+建 MR → G3 挂起",
        body["status"] == "waiting_approval" and body["gate"] == "gate_g3" and body["mr_url"],
        f"MR {body.get('mr_url')} tests={body.get('test_result')}",
    )
    mr_iid = body.get("mr_iid")

    # 5. 验证 MR 真实存在于 GitLab
    r = httpx.get(f"http://127.0.0.1:{PORTS['gitlab-bridge']}/gitlab/mrs/{DEMO_PROJECT_ID}/{mr_iid}", timeout=30)
    mr = r.json()
    check("5. GitLab 真实 MR 存在", r.status_code == 200 and mr.get("state") == "opened", f"state={mr.get('state')}")

    # 6. G3 人审批准
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{body['approval_id']}/decision",
        json={"decision": "approve", "decided_by": "代码审查者@Carol", "comment": "实现符合验收标准，LGTM"},
        timeout=10,
    )
    check("6. G3 代码审查批准", r.status_code == 200)

    # 7. 恢复 → 合入
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{run['thread_id']}/resume",
        json={"decision": "approve", "comment": ""},
        timeout=120,
    )
    done = r.json()
    check("7. 合入完成", done["status"] == "done" and done.get("merged") is True, f"branch={done.get('branch')}")

    # 8. 合入后状态校验（GitLab 侧 merged）
    r = httpx.get(f"http://127.0.0.1:{PORTS['gitlab-bridge']}/gitlab/mrs/{DEMO_PROJECT_ID}/{mr_iid}", timeout=30)
    check("8. GitLab MR 状态 merged", r.json().get("state") == "merged", f"state={r.json().get('state')}")

    # 9. 任务时间线 + 审批审计
    task = httpx.get(f"http://127.0.0.1:{PORTS['task-center']}/tasks/{task['id']}", timeout=10).json()
    types = [e["event_type"] for e in task["events"]]
    check("9. 时间线含 G2/G3 审批联动", "approval.requested" in types and "approval.decided" in types, str(types))
    print("\n── 任务时间线 ──")
    for e in task["events"]:
        print(f"  · {e['event_type']:<22} {e['by']}")

    with open(f"{TMP}/approval.log") as f:
        log = f.read()
    feishu_fail = "推送失败" in log or "推送被拒" in log
    check("10. 飞书卡片推送无失败", not feishu_fail)

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 44}\nPhase 1 演示结果: {passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    print(f"🔗 MR 链接: {body.get('mr_url')}")
    if not passed:
        for name in ("approval", "runtime", "bridge"):
            print(f"--- {name}.log ---\n{dump_log(name)}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 端到端演示（研发域闭环）")
    parser.add_argument("--gateway", action="store_true", help="LLM 走 LiteLLM 网关（真实模型）")
    parser.add_argument("--pg", action="store_true", help="审批/任务中心使用 PostgreSQL")
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
