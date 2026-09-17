#!/usr/bin/env python3
"""WP3 工程闸门 E2/E3 **真实演练**：设计不完整要打回，证据不齐不能交付。

对应课程图 41（界面状态没定义 → 打回）与图 42（产物 6/6、证据 5/6 → 仍不算完成）。

验收口径（17 号计划 WP3）：
  ① E2 用"数据/业务/接口已写、前端未定义且未声明不适用"的样例**打回**；
  ② E2 对显式"不适用 + 原因"的模块**放行**（区分经过确认的空与漏掉的空）；
  ③ E3 用"产物 6/6、证据 5/6"样例**拒绝交付**；
  ④ E3 在补齐全证据且 E1/E2 已通过后**放行**（前序闸门自动从历史补齐）；
  ⑤ 全过程留痕，可用于度量拦截率。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python scripts/drill_eng_gates_e2_e3.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
TMP = tempfile.mkdtemp(prefix="wp3-drill-")
PORT = 8051
REPORTS = ROOT / "reports"
TENANT = "drill"


def python_of(service: str) -> str:
    return str(SERVICES / service / ".venv" / "bin" / "python")


def start_gateway() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{TMP}/gateway.db", "DEFAULT_TENANT": TENANT})
    return subprocess.Popen(
        [python_of("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=SERVICES / "approval-gateway", env=env,
        stdout=open(f"{TMP}/gateway.log", "w"), stderr=subprocess.STDOUT,
    )


def wait_healthy(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


DESIGN_FULL = {
    "report": {
        "data": {"tables": ["report_export_log"], "fields": ["cost_amount", "warehouse_id"]},
        "business": {"rules": ["超 5000 行转后台"], "permissions": ["export:user"]},
        "api": {"path": "POST /api/v1/report/export", "contract": "code/msg/data"},
        "frontend": {"page": "列表页右上角导出按钮"},
        "test": {"cases": ["权限隔离", "行数一致", "空态"]},
    }
}
UX_OK = {"empty": "空态文案 + 引导", "error": "可重试提示", "loading": "骨架屏"}
UX_NA = {"empty": {"not_applicable": True, "reason": "后台任务无界面"},
         "error": {"not_applicable": True, "reason": "后台任务无界面"},
         "loading": {"not_applicable": True, "reason": "后台任务无界面"}}


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    proc = start_gateway()
    base = f"http://127.0.0.1:{PORT}"
    hdr = {"X-Tenant": TENANT}
    try:
        print("approval-gateway 启动中…")
        if not wait_healthy():
            print("❌ 网关未就绪", file=sys.stderr)
            return 1
        check("闸门服务健康", True)

        rules = {r["code"]: r for r in httpx.get(f"{base}/eng-gates", timeout=30).json()}
        check("① 三条工程闸门规则可用", set(rules) == {"E1", "E2", "E3"},
              " ".join(f"{c}({len(r['checks'])}项,{r['fingerprint']})" for c, r in sorted(rules.items())))

        # 场景 1：E2 打回——前端未定义且未声明不适用（课程图 41）
        incomplete = dict(DESIGN_FULL)
        incomplete["report"] = {k: v for k, v in DESIGN_FULL["report"].items() if k != "frontend"}
        e2_bad = httpx.post(f"{base}/eng-gates/E2/check",
                            json={"task_ref": "wp3-e2-bad",
                                  "context": {"design_docs": incomplete, "ux_states": UX_OK,
                                              "api_contract_ref": "project-rules/constraints/api-protocol.md",
                                              "data_dictionary_ref": "project-rules/design/data-dictionary.md"}},
                            headers=hdr, timeout=30).json()
        gap = next((g for g in e2_bad["gaps"] if g["id"] == "design_docs_complete"), {})
        check("② E2 打回：前端设计缺失且未声明不适用", e2_bad["blocked"] is True and "report.frontend" in gap.get("detail", ""),
              gap.get("detail", ""))

        # 场景 2：E2 打回——界面状态未定义
        e2_ux = httpx.post(f"{base}/eng-gates/E2/check",
                           json={"task_ref": "wp3-e2-ux",
                                 "context": {"design_docs": DESIGN_FULL, "ux_states": {"empty": "空态"},
                                             "api_contract_ref": "a", "data_dictionary_ref": "b"}},
                           headers=hdr, timeout=30).json()
        ux_gap = next((g for g in e2_ux["gaps"] if g["id"] == "ux_states_defined"), {})
        check("③ E2 打回：空/错/加载态未定义", e2_ux["blocked"] is True and "error" in ux_gap.get("detail", ""),
              ux_gap.get("detail", ""))

        # 场景 3：E2 放行——不适用必须带原因（后台任务）
        e2_na = httpx.post(f"{base}/eng-gates/E2/check",
                           json={"task_ref": "wp3-e2-na",
                                 "context": {"design_docs": DESIGN_FULL, "ux_states": UX_NA,
                                             "api_contract_ref": "a", "data_dictionary_ref": "b"}},
                           headers=hdr, timeout=30).json()
        check("④ E2 放行：显式声明不适用并写原因（经过确认的空）", e2_na["passed"] is True,
              f"检查项 {len(e2_na['results'])} 项全过")

        # 场景 4：E3 拒绝——产物 6/6、证据 5/6（课程图 42）
        items = [{"id": f"acc-{i}", "evidence": [f"evidence-{i}.txt"]} for i in range(1, 6)]
        items.append({"id": "acc-6"})   # 第 6 项没有证据
        e3_bad = httpx.post(f"{base}/eng-gates/E3/check",
                            json={"task_ref": "wp3-e3-bad",
                                  "context": {"artifacts": [f"a{i}" for i in range(1, 7)],
                                              "acceptance_items": items,
                                              "plan_consistency": "与方案一致", "handover": "README+回滚步骤"}},
                            headers=hdr, timeout=30).json()
        ev_gap = next((g for g in e3_bad["gaps"] if g["id"] == "acceptance_evidence"), {})
        check("⑤ E3 拒绝：产物 6/6 但证据 5/6", e3_bad["blocked"] is True and "acc-6" in ev_gap.get("detail", ""),
              ev_gap.get("detail", ""))
        prior_gap = next((g for g in e3_bad["gaps"] if g["id"] == "prior_gates_passed"), {})
        check("⑥ E3 拒绝：E1/E2 尚未执行（不许跳着交）", bool(prior_gap), prior_gap.get("detail", ""))

        # 场景 5：补齐前序闸门 → E3 自动认账并放行
        e1_ctx = {"prd_ref": "PRD v1", "sources": [{"kind": "chat", "ref": "feishu://x", "quote": "加个导出", "confirmed_by": "运营-张"}],
                  "change_targets": ["src/report/export.py"], "impact_scope": "仅新增导出接口，不影响既有调用方",
                  "rollback_plan": "保留上一版本，一键切回"}
        e1 = httpx.post(f"{base}/eng-gates/E1/check", json={"task_ref": "wp3-chain", "context": e1_ctx},
                        headers=hdr, timeout=30).json()
        e2 = httpx.post(f"{base}/eng-gates/E2/check",
                        json={"task_ref": "wp3-chain",
                              "context": {"design_docs": DESIGN_FULL, "ux_states": UX_OK,
                                          "api_contract_ref": "a", "data_dictionary_ref": "b"}},
                        headers=hdr, timeout=30).json()
        full_items = [{"id": f"acc-{i}", "evidence": [f"evidence-{i}.txt"]} for i in range(1, 7)]
        e3_ok = httpx.post(f"{base}/eng-gates/E3/check",
                           json={"task_ref": "wp3-chain",
                                 "context": {"artifacts": [f"a{i}" for i in range(1, 7)],
                                             "acceptance_items": full_items,
                                             "plan_consistency": "与方案一致", "handover": "README+回滚步骤"}},
                           headers=hdr, timeout=30).json()
        check("⑦ E1/E2 通过后 E3 放行（前序闸门从历史自动补齐）",
              e1["passed"] and e2["passed"] and e3_ok["passed"],
              f"E1={e1['passed']} E2={e2['passed']} E3={e3_ok['passed']}")

        # 场景 6：留痕与度量
        hist = httpx.get(f"{base}/eng-gates/checks", headers=hdr, timeout=30).json()
        by_code: dict[str, int] = {}
        for h in hist:
            by_code[h["code"]] = by_code.get(h["code"], 0) + 1
        blocked_n = sum(1 for h in hist if h["blocked"])
        check("⑧ 全过程留痕（拦截率可度量）", len(hist) >= 6 and blocked_n >= 3,
              f"{len(hist)} 次检查（{by_code}），阻断 {blocked_n} 次")

        # 场景 7：规则包校验进 CI 可跑
        cli = subprocess.run([python_of("approval-gateway"), str(ROOT / "scripts" / "check_project_rules.py")],
                             capture_output=True, text=True, cwd=ROOT)
        check("⑨ 项目规则包校验通过（可进 CI）", cli.returncode == 0,
              (cli.stdout.strip().splitlines() or [""])[-1])

        ok = all(c[1] for c in checks)
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"wp3-eng-gates-e2-e3-drill-{stamp}.md"
        rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
        report.write_text(f"""# WP3 工程闸门 E2/E3 演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 规则：E2 `{rules['E2']['fingerprint']}`｜E3 `{rules['E3']['fingerprint']}`
- 对应课程：图 41（状态未定义→打回）、图 42（证据 5/6→不算交付）

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 关键拦截原文

- E2 设计缺失：`{next((g['detail'] for g in e2_bad['gaps'] if g['id'] == 'design_docs_complete'), '')}`
- E2 状态缺失：`{ux_gap.get('detail', '')}`
- E3 证据缺失：`{ev_gap.get('detail', '')}`
- E3 前序闸门：`{prior_gap.get('detail', '')}`

## 结论

{'全部通过：E2 能打回不完整的设计，E3 能拒绝证据不齐的交付，前序闸门无法跳过。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
        print(f"\n报告：{report.relative_to(ROOT)}")
        print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
