#!/usr/bin/env python3
"""生产化：把平台 webhook 自动注册到 GitLab 项目（公网部署用）。

用法：
  export GITLAB_TOKEN=... PUBLIC_BASE_URL=https://platform.example.com \
         WEBHOOK_SECRET=... PROJECT_ID=sandbox6853335/prodesgin-demo
  services/agent-runtime/.venv/bin/python scripts/register_webhook.py            # 注册
  services/agent-runtime/.venv/bin/python scripts/register_webhook.py --list     # 查看
  services/agent-runtime/.venv/bin/python scripts/register_webhook.py --delete N # 删除
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
TOKEN = os.getenv("GITLAB_TOKEN", "")
PROJECT_ID = os.getenv("PROJECT_ID", os.getenv("DEMO_PROJECT_ID", "85697055"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


def _api(path: str) -> str:
    return f"{GITLAB_URL}/api/v4/projects/{PROJECT_ID}{path}"


def main() -> int:
    parser = argparse.ArgumentParser(description="平台 webhook 自动注册")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--delete", type=int, metavar="HOOK_ID")
    args = parser.parse_args()
    headers = {"PRIVATE-TOKEN": TOKEN}

    if args.list:
        hooks = httpx.get(_api("/hooks"), headers=headers, timeout=30).json()
        for h in hooks:
            print(f"  #{h['id']} {h['url']} | issues={h.get('issues_events')} mr={h.get('merge_requests_events')} pipeline={h.get('pipeline_events')}")
        print(f"共 {len(hooks)} 个 webhook")
        return 0

    if args.delete:
        r = httpx.delete(_api(f"/hooks/{args.delete}"), headers=headers, timeout=30)
        print(f"删除 webhook #{args.delete}: HTTP {r.status_code}")
        return 0 if r.status_code < 400 else 1

    if not PUBLIC_BASE_URL:
        print("缺少 PUBLIC_BASE_URL（平台公网地址，例如 https://platform.example.com）")
        return 1
    payload = {
        "url": f"{PUBLIC_BASE_URL}/webhooks/gitlab",
        "token": WEBHOOK_SECRET,
        "enable_ssl_verification": True,
        "issues_events": True,
        "merge_requests_events": True,
        "pipeline_events": True,
        "push_events": False,
    }
    r = httpx.post(_api("/hooks"), headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        print(f"❌ 注册失败 HTTP {r.status_code}: {r.text[:200]}")
        return 1
    hook = r.json()
    print(f"✅ 已注册 webhook #{hook['id']} → {hook['url']}")
    print("   生产提示：GitLab 侧应使用『签名令牌』（X-Gitlab-Signature，HMAC-SHA256），平台已支持校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
