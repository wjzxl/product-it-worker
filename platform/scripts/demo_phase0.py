#!/usr/bin/env python3
"""Phase 0 端到端演示（T0.8/T0.9）：一条需求跑完整条生产线。

链路：
  GitLab webhook(模拟) → gitlab-bridge → [事件扇出] → task-center（建任务）
  → agent-runtime（起草 PRD → 发起 G1 审批）→ approval-gateway（人审决策 → [事件扇出] → 任务状态联动）
  → agent-runtime 恢复 → 建 Issue（经 gitlab-bridge 调 GitLab API，本地用桩服务器）

运行：
  cd platform
  scripts/../services/agent-runtime/.venv/bin/python scripts/demo_phase0.py
  （任一带 httpx 的 Python 均可；各服务自动使用各自 .venv 的 Python 启动）

真实环境替换：
  - 真 GitLab：不启动桩，直接给 gitlab-bridge 配 GITLAB_URL/GITLAB_TOKEN
  - 真飞书：approval-gateway 配 FEISHU_WEBHOOK_URL / APPROVAL_WEB_BASE_URL
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")

PORTS = {
    "approval-gateway": 8000,
    "gitlab-bridge": 8001,
    "task-center": 8002,
    "agent-runtime": 8003,
}
STUB_PORT = 8099
WEBHOOK_SECRET = "s3cret-demo"
TMP = tempfile.mkdtemp(prefix="phase0-demo-")
# 真实 GitLab 模式下的目标项目路径（组/项目），可用环境变量覆盖
DEMO_PROJECT = os.getenv("DEMO_PROJECT", "sandbox/prodesgin")

results: list[str] = []
procs: list[subprocess.Popen] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    results.append((name, ok, detail))
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


# ---------- GitLab 桩服务器（本地无真 GitLab 时扮演 GitLab REST API） ----------
class GitLabStub(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        GitLabStub.requests.append((self.path, payload))
        if self.path.endswith("/issues"):
            self._send(201, {"iid": 12, "title": payload.get("title", ""), "web_url": "http://stub/issues/12"})
        else:
            self._send(404, {"message": "not found"})

    def log_message(self, *args) -> None:
        pass


def start_stub() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), GitLabStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------- 启动各服务 ----------
def service_python(name: str) -> str:
    venv = os.path.join(SERVICES, name, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


PG_URL = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform"


def start_services(gateway: bool = False, pg: bool = False, real_gitlab: bool = False) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = PG_URL if pg else f"sqlite:///{TMP}/approval.db"
    env["PUBLISH_TARGETS"] = f"http://127.0.0.1:{PORTS['task-center']}/events"
    procs.append(subprocess.Popen(
        [service_python("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["approval-gateway"])],
        cwd=os.path.join(SERVICES, "approval-gateway"), env=env,
        stdout=open(f"{TMP}/approval.log", "w"), stderr=subprocess.STDOUT,
    ))

    env = dict(os.environ)
    env["DATABASE_URL"] = PG_URL if pg else f"sqlite:///{TMP}/taskcenter.db"
    procs.append(subprocess.Popen(
        [service_python("task-center"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["task-center"])],
        cwd=os.path.join(SERVICES, "task-center"), env=env,
        stdout=open(f"{TMP}/taskcenter.log", "w"), stderr=subprocess.STDOUT,
    ))

    env = dict(os.environ)
    if real_gitlab:
        env.update({
            "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", WEBHOOK_SECRET),
            "GITLAB_URL": os.getenv("GITLAB_URL", ""),
            "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
        })
    else:
        env.update({
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "GITLAB_URL": f"http://127.0.0.1:{STUB_PORT}",
            "GITLAB_TOKEN": "demo-token",
        })
    env["PUBLISH_TARGETS"] = f"http://127.0.0.1:{PORTS['task-center']}/events"
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
    })
    if gateway:
        env.update({
            "LLM_API_KEY": "sk-litellm-master",
            "LLM_BASE_URL": f"http://127.0.0.1:4000/v1",
            "LLM_MODEL": os.getenv("DEMO_LLM_MODEL", "mock-gpt"),
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
            check(f"服务启动 {name}", False, f"超时（日志: {TMP}/{name or 'task-center'}.log）")
            return False
    return True


def dump_log(name: str) -> str:
    path = f"{TMP}/{name}.log"
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()[-1500:]


def main(gateway: bool = False, pg: bool = False, real_gitlab: bool = False) -> int:
    # 每次运行用随机 iid，保证外部引用唯一（PG 持久化下可重复运行）
    iid = random.randint(1000, 9999)
    ref = f"42#{iid}"

    if real_gitlab:
        stub = None
        print("【真实 GitLab 模式】GITLAB_URL/GITLAB_TOKEN 来自环境变量")
    else:
        stub = start_stub()
    start_services(gateway=gateway, pg=pg, real_gitlab=real_gitlab)
    print(f"服务启动中…（mode: gateway={'on' if gateway else 'off'}, pg={'on' if pg else 'off'}, iid={iid}）")
    if not wait_healthy():
        return 1
    check("四个服务健康 + GitLab 桩就绪", True)

    # 1. 模拟 GitLab webhook：新建 Issue（token 与真实模式一致，取环境变量）
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['gitlab-bridge']}/webhooks/gitlab",
        headers={"X-Gitlab-Token": os.getenv("WEBHOOK_SECRET", WEBHOOK_SECRET), "X-Gitlab-Event-UUID": f"demo-uuid-{iid}"},
        json={
            "object_kind": "issue",
            "project": {"id": 42, "path_with_namespace": DEMO_PROJECT},
            "object_attributes": {
                "iid": iid, "title": "报表导出功能",
                "description": "用户想要导出报表，支持按日期筛选", "state": "opened",
                "action": "open", "labels": [{"title": "product"}],
            },
        },
        timeout=10,
    )
    check("1. GitLab webhook 入站 → 归一化为 gitlab.issue.opened", r.status_code == 200 and "gitlab.issue.opened" in r.json()["events"], str(r.json()))
    time.sleep(1)  # 等待事件扇出

    # 2. 任务中心应有任务
    tasks = httpx.get(f"http://127.0.0.1:{PORTS['task-center']}/tasks", timeout=10).json()
    task = next((t for t in tasks if t["external_ref"] == ref), None)
    check(f"2. 事件扇出 → 任务中心自动建任务 {ref}", task is not None)
    if task is None:
        print(dump_log("bridge"))
        return 1

    # 3. Agent 领取任务
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['task-center']}/tasks/{task['id']}/transition",
        json={"to": "running", "by": "产品Agent", "detail": {"step": "draft_prd"}},
        timeout=10,
    )
    check("3. Agent 领取任务 pending→running", r.status_code == 200)

    # 4. Agent 运行时起草 PRD 并挂起在 G1
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs",
        json={
            "requirement_text": "用户想要一个可以导出报表的功能，支持按日期筛选",
            "title": "报表导出功能", "task_ref": ref, "project_id": DEMO_PROJECT,
        },
        timeout=120,
    )
    run = r.json()
    check(
        "4. Agent 起草 PRD → G1 挂起（审批单已创建）",
        r.status_code == 200 and run["status"] == "waiting_approval" and run["approval_id"],
        f"approval={run.get('approval_id')} prd_v{run.get('prd_version')}",
    )
    approval_id = run.get("approval_id", "")

    # 5. 人在审批网关做决策（模拟飞书卡片后的审批台操作）
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
        json={"decision": "approve", "decided_by": "产品负责人@Alice", "comment": "范围合理，验收标准可测"},
        timeout=10,
    )
    check("5. 人审批准（approval.decided 留痕）", r.status_code == 200 and r.json()["decision"] == "approve")
    time.sleep(1)  # 等待事件扇出 → 任务中心状态联动

    # 6. Agent 运行时恢复执行 → 经 gitlab-bridge 调 GitLab API 建 Issue
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{run['thread_id']}/resume",
        json={"decision": "approve", "comment": ""},
        timeout=120,
    )
    done = r.json()
    check("6. 恢复执行 → 建 Issue 完成（经 GitLab API）", r.status_code == 200 and done["status"] == "done", f"iid={done.get('issue', {}).get('iid')}")

    # 7. 任务时间线（全链路留痕）
    task = httpx.get(f"http://127.0.0.1:{PORTS['task-center']}/tasks/{task['id']}", timeout=10).json()
    timeline = [(e["event_type"], e["by"]) for e in task["events"]]
    check("7. 任务时间线全链路留痕", ("approval.decided" in [t[0] for t in timeline]) and task["status"] == "running", json.dumps(timeline, ensure_ascii=False))
    print("\n── 任务时间线 ──")
    for etype, by in timeline:
        print(f"  · {etype:<22} {by}")

    # 8. 审批审计
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=10).json()
    check("8. 审批审计：谁、何时、批了什么、理由", apv["decisions"][0]["decided_by"] == "产品负责人@Alice" and apv["decisions"][0]["comment"])

    # 9. 建 Issue 验证（真实模式看返回值，桩模式看请求记录）
    if real_gitlab:
        check("9. GitLab 真实 API 建 Issue 完成", done.get("issue", {}).get("iid") is not None, str(done.get("issue")))
    else:
        check("9. GitLab API 收到建 Issue 请求（含 G1-passed 标签）", any(p["labels"] == "product,G1-passed" for _, p in GitLabStub.requests))

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 44}\n演示结果: {passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    if not passed:
        for name in ("approval", "taskcenter", "bridge", "runtime"):
            print(f"--- {name}.log ---\n{dump_log(name)}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 0 端到端演示")
    parser.add_argument("--gateway", action="store_true", help="LLM 走 LiteLLM 网关（需 mock_llm_server.py 或真实 Key）")
    parser.add_argument("--pg", action="store_true", help="审批/任务中心使用 PostgreSQL（docker compose 已含）")
    parser.add_argument("--real-gitlab", action="store_true", help="使用真实 GitLab（环境变量 GITLAB_URL/GITLAB_TOKEN）")
    args = parser.parse_args()
    try:
        code = main(gateway=args.gateway, pg=args.pg, real_gitlab=args.real_gitlab)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)
