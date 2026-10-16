#!/usr/bin/env python3
"""知识库受管访问卡（新 A1 契约）：入库与检索都必须带**可验证的项目授权**。

契约变化（相对上一轮）：KB 不再接受裸的 source/content，而是要求
`project_id` + `project_version` + `repository_revision`，并回连任务中心
`GET /projects/{id}/access` 验证授权；验证不了就 503 拒绝（fail-closed）。

本卡自包含：先注册项目版本（幂等容忍 409），再入库、再检索。
用法：python3 flows/kb_check.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

KB = os.getenv("KB", "http://127.0.0.1:18003")
TC = os.getenv("TC", "http://127.0.0.1:18004")
TOKEN = os.getenv("SERVICE_AUTH_TOKEN", "pst-sut-token")
PROJECT = "camp1008"
VERSION = 1
REVISION = "camp1008rev"
SOURCE = "runbooks/camp1008-export.md"


def call(method: str, url: str, body: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Content-Type": "application/json", "X-Service-Token": TOKEN,
                 "X-Tenant": "default"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, raw[:200]


def main() -> int:
    # 作用域必须是**规范相对路径前缀**（`runbooks`），不是 glob（`runbooks/**` 不会被展开）
    version = 0
    for attempt in range(4):
        code, body = call("POST", f"{TC}/projects/{PROJECT}/versions", {
            "expected_version": attempt, "repository": "https://gitlab.example.test/camp1008/demo.git",
            "default_branch": "main", "verification_command": ["pytest", "-q"],
            "knowledge_scope": ["runbooks"], "policy_version": "v1", "max_cost_usd": 10})
        if code in (200, 201):
            version = int((body or {}).get("version") or attempt + 1)
            break
        if code != 409:                    # 409 = 该版本已存在，换下一个 expected_version
            print(f"RESULT: FAIL 注册项目版本失败 HTTP {code}: {str(body)[:180]}")
            return 1
    if not version:
        print("RESULT: FAIL 无法确定项目版本（连续 409）")
        return 1

    scope = {"project_id": PROJECT, "project_version": version, "repository_revision": REVISION}
    code, body = call("POST", f"{KB}/documents", {
        "source": SOURCE, "content": "## 导出超时处置\nWHEN 导出 p95 超过 SLO THEN 先查日期跨度再限流。",
        **scope})
    if code != 200:
        print(f"RESULT: FAIL 受管入库失败 HTTP {code}: {str(body)[:200]}")
        return 1

    code, hits = call("POST", f"{KB}/search", {"query": "导出超时怎么处置", "limit": 3, **scope})
    if code != 200:
        print(f"RESULT: FAIL 受管检索失败 HTTP {code}: {str(hits)[:200]}")
        return 1
    items = hits if isinstance(hits, list) else (hits.get("chunks") or hits.get("hits") or [])
    if not items:
        print(f"RESULT: FAIL 检索无命中：{str(hits)[:200]}")
        return 1
    src = (items[0] or {}).get("source", "")
    if not src.startswith("runbooks/"):
        print(f"RESULT: FAIL 命中来源不在授权范围：{src}")
        return 1
    print(f"RESULT: PASS 受管知识可用：{len(items)} 条命中，来源 {src}（项目 {PROJECT} v{version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
