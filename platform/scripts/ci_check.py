#!/usr/bin/env python3
"""真实 CI/CD 通路验证：经 gitlab-bridge 读取真实流水线（含作业）并尝试触发。

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT_ID=85697055
  services/agent-runtime/.venv/bin/python scripts/ci_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
PORT = 8001
PROJECT_ID = os.getenv("DEMO_PROJECT_ID", "85697055")
TMP = tempfile.mkdtemp(prefix="ci-check-")


def main() -> int:
    env = dict(os.environ)
    env.update({
        "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", "ci-check"),
        "GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
        "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=os.path.join(SERVICES, "gitlab-bridge"), env=env,
        stdout=open(f"{TMP}/bridge.log", "w"), stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print("❌ gitlab-bridge 启动失败")
            return 1

        print("① 读取真实流水线列表（经 bridge → GitLab API）")
        r = httpx.get(f"http://127.0.0.1:{PORT}/gitlab/pipelines/{PROJECT_ID}?per_page=3", timeout=30)
        pipelines = r.json() if r.status_code == 200 else []
        if pipelines:
            p = pipelines[0]
            print(f"   ✅ pipeline !{p['id']} status={p['status']} ref={p['ref']} sha={p['sha'][:8]}")
        else:
            print(f"   （无流水线：HTTP {r.status_code} {r.text[:120]}）")

        if pipelines:
            pid = pipelines[0]["id"]
            print("② 读取该流水线作业（真实回读）")
            r = httpx.get(f"http://127.0.0.1:{PORT}/gitlab/pipelines/{PROJECT_ID}/{pid}/jobs", timeout=30)
            jobs = r.json() if r.status_code == 200 else []
            print(f"   作业数：{len(jobs)}" + (f"（{[(j.get('name'), j.get('status')) for j in jobs]}）" if jobs else "（无作业分配 → 无可用 Runner）"))

            print("③ 经 bridge 触发新流水线")
            r = httpx.post(
                f"http://127.0.0.1:{PORT}/gitlab/pipelines/trigger",
                json={"project_id": PROJECT_ID, "ref": "main"}, timeout=60,
            )
            if r.status_code == 200:
                print(f"   ✅ 已触发：{r.json()}")
            else:
                detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
                print(f"   ⚠️ 触发被拒（GitLab 侧限制）：HTTP {r.status_code} — {str(detail)[:200]}")
                print("   → 处置：账号实名验证（gitlab.com）或改用自建 Runner / 内网 GitLab")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
