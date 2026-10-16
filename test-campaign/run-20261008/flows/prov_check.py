#!/usr/bin/env python3
"""来源链反查卡（WP1）：产物/task_ref → 当初是谁、在哪句话里提的。

用脚本而不是内联 curl：这条断言要构造带 sources 的审批单再反查，
塞进一行 shell 只会得到不可读的转义地狱。

用法：python3 flows/prov_check.py
"""
from __future__ import annotations

import json
import os
import urllib.request

GW = os.getenv("GW", "http://127.0.0.1:18001")
TOKEN = os.getenv("SERVICE_AUTH_TOKEN", "pst-sut-token")
TENANT = "campaign"
REF = f"camp1008-prov-{os.getpid()}"

SOURCE = {"kind": "voice", "ref": "feishu://chat/camp1008/msg/1",
          "quote": "我们对账是按月分仓看的，不是按天。", "confirmed_by": "李经理"}


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{GW}{path}", data=json.dumps(body).encode() if body else None, method=method,
        headers={"Content-Type": "application/json", "X-Service-Token": TOKEN, "X-Tenant": TENANT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


def main() -> int:
    call("POST", "/approvals", {
        "type": "GATE_REQUIREMENT_REVIEW", "stage": "来源链验证", "requester": "campaign",
        "task_ref": REF,
        # sources 属于 context_snapshot（ContextSnapshot.sources），不是顶层字段
        "context_snapshot": {"artifact_ref": "PRD", "diff_summary": "导出需求",
                             "evidence": ["https://example.test/prd"],
                             "risk": {"score": 20, "level": "L2", "factors": []},
                             "sources": [SOURCE]},
        "deadline": "2030-01-01T00:00:00+00:00",
        "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead",
                              "timeout_action": "reject"},
    })
    _, data = call("GET", f"/requirements/{REF}/provenance")
    sources = data.get("sources") or []
    hit = [s for s in sources if s.get("quote") == SOURCE["quote"]
           and s.get("confirmed_by") == SOURCE["confirmed_by"]]
    if data.get("has_provenance") and hit:
        print(f"RESULT: PASS 来源链可反查：{len(sources)} 条，quote/confirmed_by 原样可回查")
        return 0
    print(f"RESULT: FAIL 来源链反查未返回预期条目：{json.dumps(data, ensure_ascii=False)[:400]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
