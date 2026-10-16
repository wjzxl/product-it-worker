#!/usr/bin/env python3
"""UI 卡的前置数据：为审批台造出**确定性的**待审单据，其中一条带 XSS 载荷。

为什么需要它：审批台是"人做决策的界面"，它的渲染正确性只能在有数据时被看见。
其中一条审批单的 `stage` / `diff_summary` / `evidence` 故意塞入
`<img src=x onerror=...>` 与 `<script>`，用来在真实浏览器里验证转义与 CSP 是否真的生效
（单元测试断言的是字符串，这里断言的是屏幕）。

用法：
    python3 ui/seed.py --gateway http://127.0.0.1:18001 --token pst-sut-token \
        --tenant default --marker camp1008
输出：写入 ui/seed.json（单据 id），供套件与报告引用。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import urllib.request

XSS_IMG = "<img src=x onerror=alert('xss')>"
XSS_SCRIPT = "<script>alert('xss-script')</script>"


def post(url: str, body: dict, token: str, tenant: str) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Tenant": tenant,
                 "X-Service-Token": token})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def approval(ref: str, stage: str, summary: str, evidence: list) -> dict:
    return {
        "type": "GATE_REQUIREMENT_REVIEW",
        "stage": stage,
        "requester": "UI 夹具",
        "task_ref": ref,
        "context_snapshot": {
            "artifact_ref": f"PRD-{ref}",
            "diff_summary": summary,
            "evidence": evidence,
            "risk": {"score": 42, "level": "L2", "factors": ["夹具", "渲染验证"]},
            "self_review": "AI 自审：本次为 UI 渲染夹具，非真实决策",
        },
        "deadline": "2030-01-01T00:00:00+00:00",
        "escalation_policy": {"remind_after_hours": 8, "escalate_to": "lead", "timeout_action": "reject"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:18001")
    ap.add_argument("--token", default="pst-sut-token")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--marker", default="camp1008")
    ap.add_argument("--out", default=str(pathlib.Path(__file__).with_name("seed.json")))
    args = ap.parse_args()

    created = {}
    created["normal"] = post(f"{args.gateway}/approvals",
                             approval(f"{args.marker}-ui-normal", "产品域 G1（UI 夹具）",
                                      "起草 PRD v1：报表导出支持按日期筛选", ["https://example.test/prd"]),
                             args.token, args.tenant)["id"]
    created["xss"] = post(f"{args.gateway}/approvals",
                          approval(f"{args.marker}-ui-xss", f"注入载荷 {XSS_IMG}",
                                   f"需求原文含脚本 {XSS_SCRIPT} 与图片 {XSS_IMG}",
                                   [f"javascript:alert('xss')", XSS_IMG]),
                          args.token, args.tenant)["id"]
    pathlib.Path(args.out).write_text(json.dumps(created, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(json.dumps(created, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
