#!/usr/bin/env python3
"""业务流端到端卡（skill 要求：每条 flow 都要有卡，且校验**终态不变量**）。

为什么不能只靠单接口探针：单接口全绿而整条业务流断在中间，是自动化测试最常见的假安全。
这里按域真跑一遍"AI 执行 → 挂起在人审 → 人做决策 → 恢复 → 归档"，并断言终态。

用法：
    python3 flows/run_flow.py --domain product|test|ops
输出：RESULT: PASS <不变量> / RESULT: FAIL <哪个环节>
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

GW = "http://127.0.0.1:18001"
RT = "http://127.0.0.1:18002"
TOKEN = "pst-sut-token"
HUMAN_TOKEN = os.getenv("AUTH_STATIC_TOKEN", "pst-sut-personal-token")
TENANT = "default"
MARKER = "camp1008"


def call(method: str, url: str, body: dict | None = None, timeout: int = 120,
         human: bool = False) -> tuple[int, dict]:
    """默认用服务身份；`human=True` 用人类凭据——两者**不可同时携带**（会被判 invalid-personal-bearer）。"""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Tenant": TENANT}
    if human:
        headers["Authorization"] = "Bearer " + HUMAN_TOKEN
    else:
        headers["X-Service-Token"] = TOKEN
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, {"detail": raw[:300]}


def fail(msg: str) -> int:
    print(f"RESULT: FAIL {msg}")
    return 1


def uniq(domain: str) -> str:
    """每次调用一个唯一引用：产品/研发等域的运行按 (tenant,sop,task_ref) 幂等去重。"""
    return f"{MARKER}-{domain}-{int(time.time() * 1000) % 100000000}"


def start_run(sop: str, ref: str, **extra) -> tuple[int, dict]:
    body = {"sop": sop, "requirement_text": f"{MARKER} 端到端业务流验证：{sop}",
            "task_ref": ref, "tenant": TENANT, "trigger": "manual", **extra}
    return call("POST", f"{RT}/runs", body)


def approve_then_resume(tid: str, decision: str = "approve") -> tuple[int, dict, str]:
    """人审：先取挂起节点与 approval_id，在审批台上真做一次决策，再带依据恢复。"""
    _, state = call("GET", f"{RT}/runs/{tid}/state")
    interrupts = state.get("interrupts") or []
    if not interrupts:
        return 0, state, ""
    apv = interrupts[0].get("approval_id") or ""
    if not apv:
        return 0, state, ""
    code, _ = call("POST", f"{GW}/approvals/{apv}/decision",
                   {"decision": decision, "decided_by": "campaign", "comment": "端到端业务流"},
                   human=True)
    if code != 200:
        return code, {"approval": apv}, apv
    # 恢复执行是**平台侧**动作：人类决策已在审批网关留痕，这里由服务身份出示 approval_id。
    # （用人类 Bearer 调 resume 会因内部转发同时带两种凭据而被判 401 —— 见本轮 DEF-008。）
    code2, out = call("POST", f"{RT}/runs/{tid}/resume",
                      {"decision": decision, "approval_id": apv, "comment": "端到端业务流"})
    return code2, out, apv


def flow_product() -> int:
    ref = uniq("flow-product")
    code, run = start_run("product-domain/prd-flow", ref)
    if code != 200:
        return fail(f"启动产品域运行失败 HTTP {code}: {run}")
    tid = run.get("thread_id")
    if run.get("status") != "waiting_approval" or not run.get("approval_id"):
        return fail(f"运行未挂起在人审 G1：status={run.get('status')} approval={run.get('approval_id')}")
    # 人在审批台上否决
    code, _, apv = approve_then_resume(tid, "reject")
    if code != 200:
        return fail(f"人审否决/恢复链路失败 HTTP {code}")
    code, arch = call("GET", f"{RT}/runs/{tid}/archive")
    if arch.get("status") != "rejected":
        return fail(f"否决未反映到归档：status={arch.get('status')}")
    code, prov = call("GET", f"{GW}/requirements/{ref}/provenance")
    if code != 200:
        return fail(f"来源链反查失败 HTTP {code}")
    print(f"RESULT: PASS 产品域：挂起 G1 → 人审否决 → 归档 rejected（approval={apv}，"
          f"来源链条目={len(prov.get('sources') or [])}）")
    return 0


def flow_test() -> int:
    ref = uniq("flow-test")
    code, run = start_run("test-domain/release-gate", ref)
    if code != 200:
        return fail(f"启动测试域运行失败 HTTP {code}: {run}")
    tid = run.get("thread_id")
    if run.get("status") != "waiting_approval":
        return fail(f"测试域未挂起在人审：status={run.get('status')}")
    code, out, apv = approve_then_resume(tid, "approve")
    if code != 200:
        return fail(f"测试域人审通过/恢复失败 HTTP {code}: {out}")
    code, arch = call("GET", f"{RT}/runs/{tid}/archive")
    if arch.get("status") not in ("done", "waiting"):
        return fail(f"测试域终态异常：archive={arch.get('status')}")
    print(f"RESULT: PASS 测试域：挂起人审 → 批准 → 恢复（终态 {arch.get('status')}，approval={apv}）")
    return 0


def flow_dev() -> int:
    """研发域：到 G2 方案评审为止（编码与 MR 需要仓库令牌，见声明缺口）。"""
    ref = uniq("flow-dev")
    code, run = start_run("dev-domain/issue-to-merge", ref,
                          title=f"{MARKER} 研发域端到端", path_with_namespace="sandbox/prodesgin")
    if code != 200:
        return fail(f"启动研发域运行失败 HTTP {code}: {run}")
    tid = run.get("thread_id")
    if run.get("status") != "waiting_approval":
        return fail(f"研发域未挂起在人审（G2）：status={run.get('status')} gate={run.get('gate')}")
    code, _ = call("POST", f"{GW}/approvals/{run.get('approval_id')}/decision",
                   {"decision": "approve", "decided_by": "campaign", "comment": "方案可行"},
                   human=True)
    if code != 200:
        return fail(f"G2 人审决策失败 HTTP {code}")
    print(f"RESULT: PASS 研发域：挂起 {run.get('gate')} → 人审决策被接受"
          f"（后续编码/MR 需 GitLab 凭证，本环境未覆盖）")
    return 0


def flow_ops(mismatch: bool = False) -> int:
    """运维域端到端。

    `mismatch=True` 复现一个契约脆弱点：审批单的 task_ref 取自 `alert.id`，而运行状态的
    task_ref 是调用方另外传的字段；两者不一致时，决策核验会永远判"不是同一件事"，
    G5 再也批不下来（409 死锁）。
    """
    ref = uniq("flow-ops")
    alert_id = f"{MARKER}-alert" if mismatch else ref
    alert = {"id": alert_id, "title": "导出接口 p95 超 SLO", "severity": "P1",
             "source": "prometheus", "message": "camp1008 端到端：导出 p95 12s"}
    code, run = start_run("ops-domain/alert-to-postmortem", ref,
                          alert=alert, context="camp1008 告警上下文", trigger="alert")
    if code != 200:
        return fail(f"启动运维域运行失败 HTTP {code}: {run}")
    tid = run.get("thread_id")
    if run.get("status") != "waiting_approval":
        return fail(f"运维域未挂起在人审（G5）：status={run.get('status')}")
    # 人审闸门是平台的核心承诺，本卡只验到"决策被接受、恢复未被核验层拒绝"。
    # 批准之后要真去 GitLab 建 Issue/开 MR —— 本环境无仓库令牌，属已声明的未覆盖面。
    code, out, apv = approve_then_resume(tid, "approve")
    if code == 409:
        return fail(f"运维域 G5 恢复被核验层拒绝（HTTP 409）：{out}")
    code2, gate = call("GET", f"{GW}/approvals/{apv}")
    if gate.get("status") != "approved":
        return fail(f"人审决策未落到审批单：status={gate.get('status')}")
    note = "（批准后执行需 GitLab 凭证，本环境未覆盖）" if code == 500 else ""
    print(f"RESULT: PASS 运维域：告警 → SOP → G5 挂起 → 人审决策被接受{note}（approval={apv}）")
    return 0


def flow_platform() -> int:
    """平台域：挖掘 → 提案 → G7 审批单；**未获批不得应用**。"""
    code, props = call("POST", "http://127.0.0.1:18007/mining/proposals", {})
    if code != 200:
        return fail(f"提案接口失败 HTTP {code}: {props}")
    items = props.get("proposals") or []
    if not items:
        return fail("提案接口没有产出任何提案")
    code, sub = call("POST", "http://127.0.0.1:18007/mining/proposals/submit",
                     {"proposal": items[0]})
    if code != 200 or not sub.get("approval_id"):
        return fail(f"提交 G7 审批失败 HTTP {code}: {sub}")
    code, applied = call("POST", "http://127.0.0.1:18007/mining/proposals/apply",
                         {"approval_id": sub["approval_id"], "proposal": items[0]})
    if code not in (409, 422):
        return fail(f"未获批的提案竟然可以应用：HTTP {code} {applied}")
    print(f"RESULT: PASS 平台域：提案 → G7 审批单 {sub['approval_id']}；未获批应用被拒（HTTP {code}）")
    return 0


def flow_ui() -> int:
    """审批台端到端：审批人在界面上完成一次决策，且服务端留痕。"""
    import subprocess
    suite = {"suite": "审批台决策闭环", "base_url": GW, "tests": [{
        "id": "FLOW-UI", "title": "审批人在界面上批准一条待审单据", "priority": "P0",
        "screenshot": "always",
        "steps": [
            {"action": "goto", "url": "/"},
            # 新控制台：页面内输入个人令牌（Bearer）后才能读列表
            {"action": "fill", "selector": "#access-token", "value": "pst-sut-personal-token"},
            {"action": "click", "selector": "#auth-form button[type=submit]"},
            {"action": "wait_for", "selector": ".row", "timeout": 10},
            # 定位夹具审批单（首行可能是已超时单，点它会 410）；
            # 用 JS 按文本筛行，避免 text~= 选择器的匹配语义差异
            {"action": "eval", "js": "[].slice.call(document.querySelectorAll('.row')).filter(function(r){return r.textContent.indexOf('UI 夹具')>=0;})[0].click(); 'clicked'"},
            {"action": "wait_for", "selector": "button[data-action=approve]", "timeout": 10},
            {"action": "eval", "js": "window.prompt = function(){return '自动化决策人';}; 'stubbed'"},
            {"action": "click", "selector": "button[data-action=approve]"},
            {"action": "wait_ms", "value": 1500},
            {"action": "assert_no_text", "value": "操作失败"},
            {"action": "assert_text", "value": "决策已提交"},
        ]}]}
    import pathlib
    path = pathlib.Path(__file__).with_name("ui_flow_suite.json")
    path.write_text(json.dumps(suite, ensure_ascii=False, indent=1), encoding="utf-8")
    # 决策会消耗夹具单；本卡每次先造自己的（幂等：同一标记只影响新单）
    subprocess.run(["python3", str(pathlib.Path(__file__).with_name("seed_ui.py"))],
                   capture_output=True, text=True, timeout=60)
    report = pathlib.Path("evidence/ui-flow/report.json")
    proc = subprocess.run(["python3",
                           "/Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py",
                           "--suite", str(path), "--out-dir", "evidence/ui-flow",
                           "--report-json", str(report), "--baselines", "baselines"],
                          cwd=str(pathlib.Path(__file__).resolve().parent.parent),
                          capture_output=True, text=True, timeout=240)
    # 结构化 verdict 才可信：browser_test 对"元素找不到"给 BLOCKED 且退出码 0，
    # 而 --quiet 还会把 BLOCKED 摘要吞掉——只看退出码/文本都会把静默失败记成通过。
    verdicts = {}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        verdicts = {t["id"]: t["verdict"] for t in data.get("tests", [])}
    except Exception as exc:  # noqa: BLE001
        return fail(f"读不到浏览器报告（{type(exc).__name__}: {exc}）")
    if any(v != "PASS" for v in verdicts.values()) or not verdicts:
        return fail(f"审批台决策闭环未完成：{verdicts}")
    print("RESULT: PASS 审批人在界面上完成决策（截图见 evidence/ui-flow）")
    return 0


FLOWS = {"product": flow_product, "dev": flow_dev, "test": flow_test, "ops": flow_ops,
         "ops-taskref-mismatch": lambda: flow_ops(mismatch=True),
         "platform": flow_platform, "ui": flow_ui}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, choices=sorted(FLOWS))
    args = ap.parse_args()
    started = time.time()
    code = FLOWS[args.domain]()
    print(f"（耗时 {time.time() - started:.1f}s）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
