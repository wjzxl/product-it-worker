#!/usr/bin/env python3
"""多产品线推广：一键接入一条新产品线（Phase 3 ③）。

设计原则：**新产品线零信任起步**——不继承任何既有产品线的 L1 信任与历史数据，
按资料密度选择预设画像，先人工审批积累样本，再按指标自动升级。

流程：
  1. 生成产品线 SOP 覆盖文件 `platform/sop-config.d/<tenant>.yaml`（只写差异项，基线不动）；
  2. 隔离校验：approval-gateway `/trust/overview` 中该产品线的 Gate 必须为 L3（零信任）；
  3. 知识库：写入产品线专属来源空间（`<tenant>/runbooks/`），与其它产品线来源前缀隔离；
  4. GitLab（可选）：把平台 webhook 注册到该产品线项目；
  5. 生成接入报告 + 人工待办清单 `platform/reports/onboard-<tenant>-<ts>.md`。

用法：
  export APPROVAL_URL=http://127.0.0.1:8001 KB_URL=http://127.0.0.1:8003
  services/approval-gateway/.venv/bin/python scripts/onboard_product_line.py \
      --tenant line-b --product-name "支付中台" --profile strict
  # 只生成配置文件、不调用在线服务：
  ... --offline
  # 重复执行安全（幂等）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY_DIR = ROOT / "sop-config.d"
REPORT_DIR = ROOT / "reports"
BASE_CONFIG = ROOT / "sop-config.yaml"

# 预设画像：新产品线按"AI 可自主程度"选择，宁可保守
PROFILES: dict[str, dict] = {
    "strict": {
        "desc": "强监管/核心链路：样本要求高、抽样比例低、SLA 宽松",
        "trust": {"window": 30, "min_samples": 20, "l1_approve_rate": 0.95, "l2_approve_rate": 0.9, "sample_ratio": 0.2},
        "sla": {"GATE_REQUIREMENT_REVIEW": 24, "GATE_TECH_DESIGN_REVIEW": 24, "GATE_PR_REVIEW": 12,
                "GATE_RELEASE_DECISION": 8, "GATE_CHANGE_APPROVAL": 4, "GATE_POSTMORTEM_REVIEW": 48,
                "GATE_PROCESS_IMPROVEMENT": 48},
    },
    "standard": {
        "desc": "常规业务线：与平台基线一致",
        "trust": {"window": 20, "min_samples": 10, "l1_approve_rate": 0.9, "l2_approve_rate": 0.8, "sample_ratio": 0.3},
        "sla": {"GATE_REQUIREMENT_REVIEW": 24, "GATE_TECH_DESIGN_REVIEW": 24, "GATE_PR_REVIEW": 8,
                "GATE_RELEASE_DECISION": 6, "GATE_CHANGE_APPROVAL": 2, "GATE_POSTMORTEM_REVIEW": 24,
                "GATE_PROCESS_IMPROVEMENT": 24},
    },
    "fast": {
        "desc": "试验/内部工具线：样本门槛低、抽样比例高，快速验证 AI 自主度",
        "trust": {"window": 15, "min_samples": 6, "l1_approve_rate": 0.85, "l2_approve_rate": 0.75, "sample_ratio": 0.5},
        "sla": {"GATE_REQUIREMENT_REVIEW": 12, "GATE_TECH_DESIGN_REVIEW": 12, "GATE_PR_REVIEW": 6,
                "GATE_RELEASE_DECISION": 4, "GATE_CHANGE_APPROVAL": 1, "GATE_POSTMORTEM_REVIEW": 24,
                "GATE_PROCESS_IMPROVEMENT": 24},
    },
}

SEED_RUNBOOK = """# {product} 产品线运维手册（接入种子）

> 自动生成于 {ts} / 产品线 `{tenant}`。请补充真实故障处置步骤，本文件用于打通本产品线的
> 运维知识检索（来源前缀 `{tenant}/runbooks/`，与其它产品线互不干扰）。

## 服务拓扑
- 待补充：核心服务、依赖中间件、上下游调用关系。

## 常见故障与处置
| 现象 | 判定依据 | 处置动作 |
|---|---|---|
| 待补充 | 待补充 | 待补充 |

## 回滚与降级
- 回滚：待补充（发布方式 / 回滚命令 / 回滚验证）
- 降级：待补充（开关名称 / 影响面）

## 值班与升级
- 一线值班：待补充
- 升级路径：待补充
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_base() -> dict:
    if BASE_CONFIG.exists():
        return yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8")) or {}
    return {}


def write_overlay(tenant: str, profile: str, extra_sla: dict | None = None) -> tuple[Path, bool]:
    """写产品线覆盖文件（幂等：内容一致则不重写）。返回 (路径, 是否新写入)。"""
    spec = PROFILES[profile]
    sla = dict(spec["sla"])
    sla.update(extra_sla or {})
    doc = {
        "version": 1,
        "tenant": tenant,
        "profile": profile,
        "description": f"{spec['desc']}（由 onboard_product_line.py 生成，可手工微调）",
        "trust": spec["trust"],
        "gates": {gate: {"sla_hours": hours} for gate, hours in sla.items()},
    }
    body = (
        f"# 产品线覆盖配置：{tenant}（画像 {profile}）\n"
        f"# 优先级：环境变量 > 本文件 > sop-config.yaml 基线 > 内置默认\n"
        f"# 只写差异项；删除本文件即回退平台基线。\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    )
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = OVERLAY_DIR / f"{tenant}.yaml"
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return path, False
    path.write_text(body, encoding="utf-8")
    return path, True


def check_isolation(client: httpx.Client, tenant: str) -> dict:
    """隔离校验：新租户在总览里必须存在且为 L3（零信任起步）。"""
    out = {"reachable": False, "tenant_present": False, "levels": {}, "zero_trust": False, "error": None}
    try:
        resp = client.get("/trust/overview", timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # 服务未启动不应阻断接入流程
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["reachable"] = True
    row = next((r for r in rows if r.get("tenant") == tenant), None)
    if row:
        out["tenant_present"] = True
        out["levels"] = row.get("levels", {})
        out["zero_trust"] = row.get("levels", {}).get("L3", 0) > 0 and row.get("levels", {}).get("L1", 0) == 0
    out["others"] = [{"tenant": r.get("tenant"), "levels": r.get("levels")} for r in rows if r.get("tenant") != tenant]
    return out


def seed_knowledge(client: httpx.Client, tenant: str, product: str) -> dict:
    """写入产品线专属知识来源（来源前缀隔离）。"""
    source = f"{tenant}/runbooks/onboarding.md"
    content = SEED_RUNBOOK.format(product=product, tenant=tenant, ts=_now())
    try:
        resp = client.post("/documents", json={"source": source, "content": content}, timeout=60)
        resp.raise_for_status()
        return {"ok": True, "source": source, "response": resp.json()}
    except Exception as exc:
        return {"ok": False, "source": source, "error": f"{type(exc).__name__}: {exc}"}


def probe_line(client: httpx.Client, tenant: str) -> dict:
    """探测该产品线在审批网关的现状（信任视图按 X-Tenant 隔离）。"""
    try:
        resp = client.get("/trust", headers={"X-Tenant": tenant}, timeout=15)
        resp.raise_for_status()
        gates = resp.json()
        return {"ok": True, "gates": len(gates), "levels": {g["gate"]: g["level"] for g in gates}}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _drill_approvals(client: httpx.Client, tenant: str, gate: str, n: int, rejects: int = 0) -> dict:
    """在指定产品线下创建 n 条审批并给出决策，返回最终信任视图。"""
    for i in range(n):
        payload = {
            "type": gate,
            "stage": "研发域 G3",
            "requester": "dev-agent",
            "task_ref": f"{tenant}-drill-{i}",
            "context_snapshot": {
                "artifact_ref": f"mr!{i}",
                "diff_summary": "接入验收演练",
                "evidence": ["evidence-drill"],
                "risk": {"score": 10, "level": "L1", "factors": ["常规"]},
            },
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
        }
        resp = client.post("/approvals", json=payload, headers={"X-Tenant": tenant})
        resp.raise_for_status()
        apv_id = resp.json()["id"]
        decision = "reject" if i >= n - rejects else "approve"
        body = {"decision": decision, "decided_by": "接入验收员"}
        if decision == "reject":
            body["comment"] = "接入演练：模拟否决"
        resp = client.post(f"/approvals/{apv_id}/decision", json=body, headers={"X-Tenant": tenant})
        resp.raise_for_status()
    resp = client.get(f"/trust/{gate}", headers={"X-Tenant": tenant})
    resp.raise_for_status()
    view = resp.json()
    return {"level": view["level"], "samples": view["samples"], "approve_rate": view["approve_rate"]}


def verify_isolation(client: httpx.Client, tenant: str, control: str, gate: str, n: int, expected_min_samples: int) -> dict:
    """接入验收演练：证明「新产品线不继承信任」与「产品线配置独立生效」。

    做法：同一 Gate 下给新产品线与对照产品线各投 n 条审批并全部批准——
    对照线走平台基线（min_samples=10）应升到 L1；新产品线按自己的画像（如 strict 需 20 条）
    必须**仍不是 L1**。随后在新产品线制造一次否决，验证惩罚不扩散到对照线。
    """
    out: dict = {"gate": gate, "n": n, "expected_min_samples": expected_min_samples}
    try:
        before = {r["tenant"]: r["levels"] for r in client.get("/trust/overview", timeout=15).json()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["before"] = before

    out["tenant_run"] = _drill_approvals(client, tenant, gate, n)
    out["control_run"] = _drill_approvals(client, control, gate, n)

    # 惩罚隔离：新产品线否决一条 → 只有它掉到 L3
    out["tenant_after_reject"] = _drill_approvals(client, tenant, gate, 1, rejects=1)
    control_after = client.get(f"/trust/{gate}", headers={"X-Tenant": control}).json()
    out["control_after_reject"] = {"level": control_after["level"], "samples": control_after["samples"]}

    out["checks"] = {
        "新线未继承信任（非 L1）": out["tenant_run"]["level"] != "L1",
        "对照线按基线升到 L1": out["control_run"]["level"] == "L1",
        "样本量符合各自配置": out["tenant_run"]["samples"] == n and out["control_run"]["samples"] == n,
        "惩罚不扩散（对照线仍 L1）": out["control_after_reject"]["level"] == "L1",
        "被否决线回到 L3": out["tenant_after_reject"]["level"] == "L3",
    }
    out["ok"] = all(out["checks"].values())
    return out


def register_webhook_if_needed(tenant: str, project: str | None, secret: str, base_url: str) -> dict:
    """把平台 webhook 注册到产品线 GitLab 项目（可选步骤）。"""
    token = os.getenv("GITLAB_TOKEN", "")
    if not project:
        return {"skipped": "未提供 --gitlab-project"}
    if not token:
        return {"skipped": "缺少 GITLAB_TOKEN"}
    if not base_url:
        return {"skipped": "缺少 PUBLIC_BASE_URL（公网可达地址）"}
    gitlab = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    url = f"{gitlab}/api/v4/projects/{project}/hooks"
    payload = {
        "url": f"{base_url}/webhooks/gitlab",
        "token": secret,
        "issues_events": True,
        "merge_requests_events": True,
        "pipeline_events": True,
        "note_events": True,
    }
    try:
        resp = httpx.post(url, headers={"PRIVATE-TOKEN": token}, json=payload, timeout=30)
        if resp.status_code in (400, 409) and "already been taken" in resp.text:
            return {"ok": True, "note": "webhook 已存在（跳过）"}
        resp.raise_for_status()
        return {"ok": True, "hook_id": resp.json().get("id")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def render_report(tenant: str, product: str, profile: str, data: dict) -> str:
    overlay = data["overlay"]
    lines = [
        f"# 产品线接入报告 · {product}（`{tenant}`）",
        "",
        f"- 生成时间：{_now()}",
        f"- 画像：**{profile}** — {PROFILES[profile]['desc']}",
        f"- 覆盖配置：`{overlay['path']}`（{'新写入' if overlay['written'] else '已存在且一致，跳过'}）",
        "",
        "## 1. 产品线参数（覆盖基线）",
        "",
        "| 参数 | 本产品线 | 平台基线 |",
        "|---|---|---|",
    ]
    base_trust = (_load_base().get("trust") or {})
    for key, value in PROFILES[profile]["trust"].items():
        lines.append(f"| {key} | {value} | {base_trust.get(key, '—')} |")
    lines += ["", "## 2. 信任隔离校验（零信任起步）", ""]
    iso = data["isolation"]
    if not iso.get("reachable"):
        lines.append(f"- ⏸️ 未连接审批网关（{iso.get('error')}）——启动服务后重跑本脚本完成校验")
    elif iso.get("zero_trust"):
        lines.append(f"- ✅ 该产品线 Gate 等级分布 {iso['levels']}：全部 L3，**未继承其它产品线信任**")
    elif iso.get("tenant_present"):
        lines.append(f"- ⚠️ 该产品线已有信任记录 {iso['levels']}（非零信任起步，请确认是否符合预期）")
    else:
        lines.append("- ⏸️ 该产品线尚无审批记录：首次提交审批后即出现在总览中（预计 L3）")
    if iso.get("others"):
        lines.append(f"- 其它产品线（应互不影响）：{json.dumps(iso['others'], ensure_ascii=False)}")

    lines += ["", "## 3. 知识库来源空间", ""]
    kb = data["kb"]
    if kb.get("ok"):
        lines.append(f"- ✅ 已写入 `{kb['source']}`（本产品线知识前缀 `{tenant}/`，与其它产品线隔离）")
    else:
        lines.append(f"- ⏸️ 未写入（{kb.get('error') or kb.get('skipped')}）")

    lines += ["", "## 4. GitLab 接入", ""]
    hook = data["webhook"]
    if hook.get("ok"):
        lines.append(f"- ✅ webhook 已注册（hook_id={hook.get('hook_id')}）")
    else:
        lines.append(f"- ⏸️ 未注册（{hook.get('skipped') or hook.get('error')}）")

    if data.get("verify"):
        v = data["verify"]
        lines += ["", "## 5. 隔离验收演练（真实服务上执行）", ""]
        if not v.get("ok") and v.get("error"):
            lines.append(f"- ❌ 演练失败：{v['error']}")
        else:
            lines += [
                f"- 演练 Gate：`{v['gate']}`；新产品线与对照线（`{v.get('control_tenant', 'drill-control')}`）"
                f"各投 {v['n']} 条审批并全部批准",
                f"- 本产品线画像要求 `min_samples={v['expected_min_samples']}` → 本次结果 "
                f"**{v['tenant_run']['level']}**（{v['tenant_run']['samples']} 样本，通过率 {v['tenant_run']['approve_rate']:.0%}）",
                f"- 对照线走平台基线 → **{v['control_run']['level']}**（{v['control_run']['samples']} 样本）",
                f"- 本产品线否决一条后：本线 **{v['tenant_after_reject']['level']}**，"
                f"对照线 **{v['control_after_reject']['level']}**（惩罚不扩散）",
                "",
                "| 验收项 | 结果 |",
                "|---|---|",
            ]
            for name, ok in (v.get("checks") or {}).items():
                lines.append(f"| {name} | {'✅' if ok else '❌'} |")

    lines += ["", "## 6. 人工待办清单（接入不可全自动的部分）", ""]
    lines += [
        "- [ ] GitLab 项目 CI 变量：`PLATFORM_BASE_URL`、`WEBHOOK_SECRET`（与平台一致）",
        "- [ ] SSO：为该产品线评审人授予 `OIDC_REQUIRED_ROLE` 对应角色",
        "- [ ] IM：把飞书群机器人 webhook 配到该产品线通知路由",
        "- [ ] 知识库：补充真实 Runbook/SOP 文档到 `%s/runbooks/`" % tenant,
        "- [ ] 首轮演练：提交 1 个真实需求走完 G1→G4，确认人工闸门与留痕正常",
        "",
        "## 7. 信任升级路径（AI 自主度提升）",
        "",
        f"- 起步 L3 全人工 → 累计 {PROFILES[profile]['trust']['min_samples']} 条决策且通过率 ≥ "
        f"{PROFILES[profile]['trust']['l1_approve_rate']:.0%} 且近 5 条无退回 → L1 全自动放行；",
        f"- 通过率 ≥ {PROFILES[profile]['trust']['l2_approve_rate']:.0%} → L2（按 "
        f"{PROFILES[profile]['trust']['sample_ratio']:.0%} 抽样人审）；",
        "- 任一退回/否决 → 本产品线该 Gate 立即回到 L3（失败即回升），不影响其它产品线。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="产品线一键接入（多产品线推广）")
    parser.add_argument("--tenant", required=True, help="产品线标识（小写字母/数字/中划线，用作 X-Tenant）")
    parser.add_argument("--product-name", default="", help="产品线中文名（报告展示用）")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="standard")
    parser.add_argument("--gitlab-project", default="", help="产品线 GitLab 项目 path 或 id")
    parser.add_argument("--offline", action="store_true", help="不调用在线服务，只生成配置与报告")
    parser.add_argument("--sla", default="", help="追加/覆盖 Gate SLA，如 GATE_PR_REVIEW=4,GATE_RELEASE_DECISION=2")
    parser.add_argument("--verify", action="store_true", help="接入后跑隔离验收演练（写入演练数据，请用演练库/演练项目）")
    parser.add_argument("--verify-gate", default="GATE_PR_REVIEW")
    parser.add_argument("--verify-n", type=int, default=12, help="演练审批条数（应大于基线 min_samples、小于本画像 min_samples）")
    parser.add_argument("--control-tenant", default="drill-control", help="对照产品线（走平台基线，验证配置与惩罚隔离）")
    args = parser.parse_args()

    tenant = args.tenant.strip().lower()
    if not tenant or not all(c.isalnum() or c in "-_" for c in tenant):
        print(f"❌ 非法租户标识：{args.tenant!r}（只允许字母/数字/中划线/下划线）", file=sys.stderr)
        return 2
    extra_sla = {}
    for chunk in filter(None, (c.strip() for c in args.sla.split(","))):
        if "=" not in chunk:
            print(f"❌ --sla 格式应为 GATE_X=小时，收到 {chunk!r}", file=sys.stderr)
            return 2
        gate, hours = chunk.split("=", 1)
        extra_sla[gate.strip()] = float(hours)

    product = args.product_name or tenant
    started = time.time()
    path, written = write_overlay(tenant, args.profile, extra_sla)
    print(f"① 覆盖配置 {path}（{'新写入' if written else '已存在且一致'}）")

    data: dict = {"overlay": {"path": str(path.relative_to(ROOT)), "written": written}}
    if args.offline:
        data["isolation"] = {"reachable": False, "error": "offline 模式跳过"}
        data["kb"] = {"skipped": "offline 模式跳过"}
        data["webhook"] = {"skipped": "offline 模式跳过"}
    else:
        approval_url = os.getenv("APPROVAL_URL", "http://127.0.0.1:8001").rstrip("/")
        kb_url = os.getenv("KB_URL", "http://127.0.0.1:8003").rstrip("/")
        with httpx.Client(base_url=approval_url) as client:
            data["isolation"] = check_isolation(client, tenant)
            data["probe"] = probe_line(client, tenant)
        with httpx.Client(base_url=kb_url) as kb_client:
            data["kb"] = seed_knowledge(kb_client, tenant, product)
        data["webhook"] = register_webhook_if_needed(
            tenant, args.gitlab_project or None,
            os.getenv("WEBHOOK_SECRET", ""), os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        )
        if args.verify:
            with httpx.Client(base_url=approval_url) as client:
                data["verify"] = verify_isolation(
                    client, tenant, args.control_tenant, args.verify_gate, args.verify_n,
                    PROFILES[args.profile]["trust"]["min_samples"],
                )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = REPORT_DIR / f"onboard-{tenant}-{stamp}.md"
    report.write_text(render_report(tenant, product, args.profile, data), encoding="utf-8")

    iso = data["isolation"]
    if not iso.get("reachable"):
        iso_state = "⏸️ 未连接网关"
    elif iso.get("zero_trust"):
        iso_state = "✅ 零信任起步（全部 L3，未继承其它产品线信任）"
    elif iso.get("tenant_present"):
        iso_state = "⚠️ 已有信任记录，非零信任起步"
    else:
        iso_state = "⏸️ 尚无审批记录（首次提交后出现，预计 L3）"
    print(f"② 隔离校验：{iso_state}")
    print(f"③ 知识库：{'✅ ' + data['kb']['source'] if data['kb'].get('ok') else '⏸️ 跳过'}")
    print(f"④ GitLab：{'✅ hook ' + str(data['webhook'].get('hook_id')) if data['webhook'].get('ok') else '⏸️ 跳过'}")
    if args.verify:
        v = data.get("verify") or {}
        if v.get("ok"):
            print(f"⑤ 隔离验收演练：✅ 全部通过（新线非 L1={v['tenant_run']['level']}，"
                  f"对照线={v['control_run']['level']}，否决后对照线={v['control_after_reject']['level']}）")
        else:
            print(f"⑤ 隔离验收演练：❌ 未通过 {v.get('checks') or v.get('error')}")
    print(f"⑥ 接入报告 {report.relative_to(ROOT)}（耗时 {time.time() - started:.1f}s）")
    print("\n下一步人工待办：CI 变量 / SSO 角色 / 飞书群机器人 / 首轮 G1→G4 演练（详见报告第 5 节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
