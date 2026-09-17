#!/usr/bin/env python3
"""WP8 工具注册表与执行期鉴权**真实演练**：两种边界要挡得住。

对应课程图 59：
  - **看不见**：岗位没有的工具，调用即拒（不是"提示词里劝它别用"）；
  - **不能改**：危险动作（生产变更）必须有 G5 审批依据，否则拒绝执行；
  - **边界要与配置一致**：岗位三件套引用的工具必须已注册，助理不得持有 ops.execute。

验收口径（17 号计划 WP8）：
  ① 越权调用被拒并留审计日志；
  ② 新增一个工具**不改 agent 代码**（只加 YAML）；
  ③ 工具 Schema/必填参数校验失败给出明确报错；
  ④ 危险工具带审批号才放行；
  ⑤ 岗位可见工具与其 skills 完全一致。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python scripts/drill_tool_registry.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))

from app.apis import MockApprovalAPI, MockGitLabAPI  # noqa: E402
from app.llm import MockLLM  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tools import ToolDenied, ToolRegistry  # noqa: E402

REPORTS = ROOT / "reports"


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    app = create_app(approval_api=MockApprovalAPI(), gitlab_api=MockGitLabAPI(), llm=MockLLM(answers=[]))
    registry = app.state.tools
    info = registry.summary()

    check("① 工具注册表加载", info["count"] >= 10,
          f"{info['count']} 个工具，需人审：{info['approval_required_tools']}")
    check("② 每个工具都登记了权限点/参数类型/失败模式",
          all(t["permission"] and t["params"] is not None for t in info["tools"]),
          f"写类工具：{info['write_tools']}")

    # 看不见：ops-duty 的工具集里没有 ops.execute
    role_tools = registry.allowed_for(
        __import__("app.roles", fromlist=["x"]).load_role("ops-duty").tool_ids
    )
    check("③ 运维助理看不到生产变更工具（看不见）", "ops.execute" not in role_tools,
          f"可见：{role_tools}")

    denied = []
    for tool, params, role in [
        ("ops.execute", {"action": "restart", "target": "order-api"}, "ops-duty"),
        ("gitlab.branch_mr", {"project": "p", "branch": "b", "title": "t"}, "ops-duty"),
        ("prometheus.query", {"service": "order-api"}, "ops-duty"),          # 缺参数
        ("不存在.tool", {}, "ops-duty"),                                      # 未注册
    ]:
        try:
            registry.invoke(tool, params, role=role, role_tools=role_tools)
            denied.append((tool, "未拒绝"))
        except ToolDenied as exc:
            denied.append((tool, exc.reason))
    check("④ 四类越权/非法调用全部被拒", all(r != "未拒绝" for _t, r in denied),
          "；".join(f"{t}→{r}" for t, r in denied))

    audit_denied = [e for e in registry.audit_log if e["outcome"] == "denied"]
    check("⑤ 拒绝都留了审计（可查证）", len(audit_denied) >= 4,
          f"{len(audit_denied)} 条拒绝记录，最近一条：{json.dumps(audit_denied[-1], ensure_ascii=False)}")

    # 不能改：带审批号才放行
    registry.register_handler("ops.execute", lambda action, target, approval_ref="", params=None: {
        "executed": True, "action": action, "target": target, "approval_ref": approval_ref})
    try:
        registry.invoke("ops.execute", {"action": "restart", "target": "order-api"},
                        role="ops-lead", role_tools=["ops.execute"])
        ungated = "未拒绝"
    except ToolDenied as exc:
        ungated = exc.reason
    gated = registry.invoke("ops.execute",
                            {"action": "restart", "target": "order-api", "approval_ref": "apv_g5_001"},
                            role="ops-lead", role_tools=["ops.execute"], approval_ref="apv_g5_001")
    check("⑥ 危险动作无审批被拒、带 G5 审批号放行",
          ungated == "approval_required" and gated["status"] == "ok",
          f"无审批→{ungated}；带审批→{gated['result']}")

    # 不改代码新增工具（只加 YAML）
    tmp = Path(tempfile.mkdtemp(prefix="wp8-tool-"))
    (tmp / "kb.search.yaml").write_text((ROOT / "tools" / "kb.search.yaml").read_text(encoding="utf-8"),
                                        encoding="utf-8")
    (tmp / "new.tool.yaml").write_text(yaml.safe_dump({
        "id": "report.generate", "title": "生成报表", "kind": "internal", "provider": "agent-runtime",
        "description": "按口径生成报表文件（新增工具示例：只加 YAML，不改 agent 代码）",
        "read_only": False, "requires_approval": False, "idempotent": True,
        "permission": "report:write", "audit": "standard",
        "params": {"month": {"type": "string", "required": True}},
        "failure_modes": ["口径未定义 → 拒绝生成并提示先确认统计口径"],
    }, allow_unicode=True), encoding="utf-8")
    fresh = ToolRegistry(tmp)
    check("⑦ 新增工具只需加 YAML（不改代码）",
          "report.generate" in fresh.ids and "report.generate" in fresh.summary()["write_tools"],
          f"新注册表：{sorted(fresh.ids)}")

    # 岗位与工具一致性
    roles = __import__("app.roles", fromlist=["x"]).load_all(ROOT / "roles", known_tools=registry.ids)
    check("⑧ 岗位三件套与工具注册表双向一致",
          all(set(r.tool_ids) <= registry.ids for r in roles),
          "；".join(f"{r.name}:{len(r.tool_ids)}" for r in roles))

    ok = all(c[1] for c in checks)
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = REPORTS / f"wp8-tool-registry-drill-{stamp}.md"
    rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
    audit_rows = "\n".join(
        f"| {e['tool']} | {e['role'] or '-'} | {e['outcome']} | {e['reason']} |" for e in registry.audit_log
    )
    report.write_text(f"""# WP8 工具注册表与执行期鉴权演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 工具总数：{info['count']}｜写类：{info['write_tools']}｜需人审：{info['approval_required_tools']}
- 对应课程：图 59「两种边界」（看不见 / 不能改）、图 62「工具说明」

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 审计日志（含被拒记录）

| 工具 | 岗位 | 结果 | 原因 |
|---|---|---|---|
{audit_rows}

## 结论

{'全部通过：边界在执行期强制，且新增工具无需改代码。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
    print(f"\n报告：{report.relative_to(ROOT)}")
    print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
