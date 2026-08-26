#!/usr/bin/env python3
"""Phase 1 批量试点：真实 GitLab 批量创建 Issue（auto-dev 标签）→ 自动触发研发域流水线
→ 自动人审（记录决策耗时）→ 合入 → 收集质量指标，输出试点报告。

用法：
  export GITLAB_TOKEN=... DEMO_PROJECT="sandbox6853335/prodesgin-demo" \
         FEISHU_WEBHOOK_URL=... WEBHOOK_SECRET=...
  services/agent-runtime/.venv/bin/python scripts/batch_pilot.py --gateway --pg --count 5
"""
from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
REPORTS = os.path.join(ROOT, "reports")

PORTS = {"approval-gateway": 8000, "gitlab-bridge": 8001, "task-center": 8002, "agent-runtime": 8003}
DEMO_PROJECT = os.getenv("DEMO_PROJECT", "sandbox6853335/prodesgin-demo")
DEMO_PROJECT_ID = int(os.getenv("DEMO_PROJECT_ID", "85697055"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "phase1-secret")
TMP = tempfile.mkdtemp(prefix="pilot-")

procs: list[subprocess.Popen] = []

# 试点功能清单（每个都是小模块 + 测试，DeepSeek 可在基线仓库内独立实现）
FEATURES = [
    {"title": "按类别汇总报表", "prd": "## 验收标准\n- WHEN 传入多条报表数据 THEN summarize_by_category 按类别聚合数值并返回字典\n- WHEN 数据为空 THEN 返回空字典\n- 数值保留两位小数\n"},
    {"title": "每日趋势统计", "prd": "## 验收标准\n- WHEN 传入日期+数值数据 THEN daily_trend 返回按日期升序的列表\n- WHEN 同一天多条记录 THEN 数值累加\n- 输出字段：日期、数值\n"},
    {"title": "重复数据去重", "prd": "## 验收标准\n- WHEN 同日存在多条记录 THEN dedupe_by_date 保留最后一条\n- WHEN 数据为空 THEN 返回空列表\n- 不修改输入列表\n"},
    {"title": "报表数据量统计", "prd": "## 验收标准\n- WHEN 传入数据列表 THEN count_rows 返回行数\n- WHEN 传入空列表 THEN 返回 0\n"},
    {"title": "导出文件名校验", "prd": "## 验收标准\n- WHEN 给定起止日期 THEN build_filename 返回 报表_{start}_{end}.csv\n- WHEN 日期为空 THEN 使用 today 兜底\n"},
    {"title": "数据行有效性检查", "prd": "## 验收标准\n- WHEN 行缺少必要字段 THEN validate_row 返回 False 并给出原因\n- WHEN 字段齐全 THEN 返回 True\n- 必要字段：日期/类别/数值\n"},
    {"title": "数值千分位格式化", "prd": "## 验收标准\n- WHEN 传入 1234567.8 THEN format_thousands 返回 1,234,567.8\n- WHEN 传入整数 THEN 不带小数\n- WHEN 传入负数 THEN 保留负号\n"},
    {"title": "日期范围重叠检测", "prd": "## 验收标准\n- WHEN 两个日期范围有交集 THEN has_overlap 返回 True\n- WHEN 首尾相接（end==start）THEN 视为重叠\n- WHEN 任一段无效 THEN 返回 False\n"},
    {"title": "报表字段重命名", "prd": "## 验收标准\n- WHEN 传入字段映射 THEN rename_columns 重命名并保留未映射字段\n- WHEN 映射为空 THEN 原样返回\n- 不修改输入列表", "review_touch": "revise"},
    {"title": "报表周汇总", "prd": "## 验收标准\n- WHEN 传入日期+数值数据 THEN weekly_summary 按 ISO 周聚合数值并返回 {年-周: 合计}\n- WHEN 同周多条记录 THEN 数值累加\n- WHEN 数据为空 THEN 返回空字典", "review_touch": "revise"},
]


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
    env["AGENT_RUNTIME_URL"] = f"http://127.0.0.1:{PORTS['agent-runtime']}"  # auto-dev 闭环触发
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
            print(f"❌ 服务 {name} 启动超时")
            return False
    return True


def poll_state(thread_id: str, gates: list[str], timeout: float) -> dict:
    """轮询直到进入任一目标 Gate 或流程结束；超时返回 timeout。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/state", timeout=10)
            snap = r.json()
        except httpx.HTTPError:
            time.sleep(5)
            continue
        values = snap.get("values", {})
        if snap.get("next"):
            gate = next((g for g in gates if g in snap["next"]), None)
            if gate:
                return {"status": f"waiting_{gate}", **snap}
        else:
            if values.get("merged"):
                return {"status": "merged", **snap}
            return {"status": values.get("gate3_decision") or values.get("gate2_decision") or "done", **snap}
        time.sleep(5)
    return {"status": "timeout"}


def decide(approval_id: str, decision: str, by: str) -> float:
    """批准/拒绝一个 Gate，返回决策耗时（秒）。"""
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=10).json()
    created = datetime.fromisoformat(apv["created_at"].replace("Z", "+00:00"))
    httpx.post(
        f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
        json={"decision": decision, "decided_by": by, "comment": "批量试点自动审批"},
        timeout=10,
    )
    return (datetime.now(timezone.utc) - created).total_seconds()


def run_issue(feat: dict) -> dict:
    title, prd = feat["title"], feat["prd"]
    t0 = time.time()
    # 1. 真实创建 Issue（auto-dev 标签 → 闭环触发）
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['gitlab-bridge']}/gitlab/issues",
        json={"project_id": DEMO_PROJECT, "title": title, "description": prd, "labels": ["product", "auto-dev"]},
        timeout=60,
    )
    if r.status_code >= 400 or not r.content:
        raise RuntimeError(f"创建 Issue 失败 HTTP {r.status_code}: {r.text[:200]}")
    issue = r.json()
    iid = issue.get("iid")
    ref = f"{DEMO_PROJECT_ID}#{iid}"
    thread_id = ref.replace("#", "-")

    # 2. 模拟 GitLab 投递 webhook（真实部署由 webhook 完成；本地无公网可达地址）
    httpx.post(
        f"http://127.0.0.1:{PORTS['gitlab-bridge']}/webhooks/gitlab",
        headers={"X-Gitlab-Token": WEBHOOK_SECRET, "X-Gitlab-Event-UUID": f"pilot-{iid}"},
        json={
            "object_kind": "issue",
            "project": {"id": DEMO_PROJECT_ID, "path_with_namespace": DEMO_PROJECT},
            "object_attributes": {
                "iid": iid, "title": title, "description": prd, "state": "opened",
                "action": "open", "labels": [{"title": "product"}, {"title": "auto-dev"}],
            },
        },
        timeout=30,
    )

    # 3. 等待 auto-dev 自动触发 → G2 挂起
    snap = poll_state(thread_id, ["gate_g2"], timeout=120)
    if snap["status"] != "waiting_gate_g2":
        return {"issue_iid": iid, "title": title, "status": f"启动失败:{snap['status']}", "lead_time_s": time.time() - t0}
    aid2 = snap["values"].get("approval_id_g2")
    # 可选：真实"退回修订 → 再审批"演练（G2 先 revise，等新方案再批准）
    if feat.get("review_touch") == "revise":
        decide(aid2, "revise", "批量试点审批员（评审意见：请补充字段映射为空时的边界说明）")
        httpx.post(
            f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/resume",
            json={"decision": "revise", "comment": "补充字段映射为空时的边界说明"}, timeout=120,
        )
        snap = poll_state(thread_id, ["gate_g2"], timeout=120)
        if snap["status"] != "waiting_gate_g2":
            return {"issue_iid": iid, "title": title, "status": f"修订后未达G2:{snap['status']}", "lead_time_s": time.time() - t0}
        aid2 = snap["values"].get("approval_id_g2")
    g2_lat = decide(aid2, "approve", "批量试点审批员")
    # 恢复执行 → 进入 AI 编码阶段
    httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/resume",
        json={"decision": "approve", "comment": ""}, timeout=300,
    )

    # 4. 等待编码+MR → G3 挂起（DeepSeek 多轮修复可能耗时较长）
    snap = poll_state(thread_id, ["gate_g3"], timeout=1500)
    if snap["status"] != "waiting_gate_g3":
        return {"issue_iid": iid, "title": title, "status": f"编码失败:{snap['status']}", "lead_time_s": time.time() - t0, "g2_latency_s": round(g2_lat, 1)}
    aid3 = snap["values"].get("approval_id_g3")
    g3_lat = decide(aid3, "approve", "批量试点审批员")

    # 5. 恢复 → 合入
    r = httpx.post(
        f"http://127.0.0.1:{PORTS['agent-runtime']}/runs/{thread_id}/resume",
        json={"decision": "approve", "comment": ""}, timeout=120,
    )
    done = r.json()
    merged = done.get("status") == "done" and done.get("merged") is True

    # 6. 关闭真实 Issue
    try:
        httpx.put(
            f"https://gitlab.com/api/v4/projects/{DEMO_PROJECT_ID}/issues/{iid}",
            headers={"PRIVATE-TOKEN": os.getenv("GITLAB_TOKEN", "")},
            params={"state_event": "close"}, timeout=15,
        )
    except httpx.HTTPError:
        pass

    return {
        "issue_iid": iid,
        "title": title,
        "status": "merged" if merged else "failed",
        "mr_iid": done.get("mr_iid"),
        "mr_url": done.get("mr_url", ""),
        "tests": done.get("test_result", ""),
        "branch": done.get("branch", ""),
        "g2_latency_s": round(g2_lat, 1),
        "g3_latency_s": round(g3_lat, 1),
        "lead_time_s": round(time.time() - t0, 1),
    }


def main(gateway: bool, pg: bool, count: int, offset: int = 0) -> int:
    os.makedirs(REPORTS, exist_ok=True)
    start_services(gateway, pg)
    print(f"服务启动中…（batch={count} offset={offset}）")
    if not wait_healthy():
        return 1

    rows = []
    for i, feat in enumerate(FEATURES[offset:offset + count], 1):
        print(f"\n── 试点 {offset + i}/{len(FEATURES)}: {feat['title']} ──")
        try:
            row = run_issue(feat)
        except Exception as exc:  # 单条失败不中断批次
            row = {"issue_iid": "-", "title": feat["title"], "status": f"异常:{type(exc).__name__}", "lead_time_s": 0}
            print(f"  ❌ {exc}")
        rows.append(row)
        print(f"  {row}")

    merged = [r for r in rows if r["status"] == "merged"]
    lat2 = [r["g2_latency_s"] for r in merged if "g2_latency_s" in r]
    lat3 = [r["g3_latency_s"] for r in merged if "g3_latency_s" in r]
    lead = [r["lead_time_s"] for r in rows]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": DEMO_PROJECT,
        "issues": len(rows),
        "merged": len(merged),
        "merge_rate": f"{len(merged)}/{len(rows)}",
        "avg_lead_time_s": round(sum(lead) / len(lead), 1) if lead else None,
        "avg_g2_decision_s": round(sum(lat2) / len(lat2), 1) if lat2 else None,
        "avg_g3_decision_s": round(sum(lat3) / len(lat3), 1) if lat3 else None,
        "rows": rows,
    }

    md = [f"# Phase 1 批量试点报告（{report['generated_at'][:10]}）",
          "",
          f"- 项目：{DEMO_PROJECT}",
          f"- Issue 数：{report['issues']} ｜ 合入：{report['merge_rate']}",
          f"- 平均端到端耗时：{report['avg_lead_time_s']}s ｜ 平均 G2 决策：{report['avg_g2_decision_s']}s ｜ 平均 G3 决策：{report['avg_g3_decision_s']}s",
          "",
          "| Issue | 功能 | 状态 | MR | 测试 | G2 决策(s) | G3 决策(s) | 耗时(s) |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| #{r.get('issue_iid', '-')} | {r['title']} | {r['status']} | !{r.get('mr_iid', '-')} | {r.get('tests', '-')} | {r.get('g2_latency_s', '-')} | {r.get('g3_latency_s', '-')} | {r.get('lead_time_s', '-')} |")
    report_path = os.path.join(REPORTS, f"phase1-pilot-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print(f"\n{'=' * 60}\n试点报告已写入 {report_path}")
    print(f"合入率: {report['merge_rate']} | 平均端到端: {report['avg_lead_time_s']}s | G2 决策: {report['avg_g2_decision_s']}s | G3 决策: {report['avg_g3_decision_s']}s")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 批量试点")
    parser.add_argument("--gateway", action="store_true")
    parser.add_argument("--pg", action="store_true")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0, help="功能清单起始下标（补跑用）")
    args = parser.parse_args()
    code = 1
    try:
        code = main(gateway=args.gateway, pg=args.pg, count=args.count, offset=args.offset)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)
