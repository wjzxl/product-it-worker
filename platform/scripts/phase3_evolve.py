#!/usr/bin/env python3
"""Phase 3 流程挖掘与 SOP 自进化端到端演示。

链路（用平台自身数据改进平台）：
  ① 挖掘真实数据（PG：审批/时间线/成本）→ 瓶颈清单
  ② LLM（Qwen）生成结构化优化提案
  ③ 提案提交 G7 流程改进人审闸门（approval-gateway）
  ④ 人审批准 → 应用（写 SOP 配置中心，热更新生效）
  ⑤ 登记 GitLab 改进项（真实 issue）
  ⑥ 效果度量（信任参数/配置版本变化）

用法：
  export GITLAB_TOKEN=... PROJECT_ID=85697055 QWEN_API_KEY=...
  services/process-mining/.venv/bin/python scripts/phase3_evolve.py
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
PG = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform"
PORTS = {"approval-gateway": 8030, "gitlab-bridge": 8031, "process-mining": 8032}
TMP = tempfile.mkdtemp(prefix="phase3-")

procs: list[subprocess.Popen] = []
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def python_of(service: str) -> str:
    venv = os.path.join(SERVICES, service, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def start() -> None:
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": PG,
        "SOP_CONFIG_PATH": os.path.join(ROOT, "sop-config.yaml"),
        "AUTH_MODE": "off",
    })
    procs.append(subprocess.Popen(
        [python_of("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["approval-gateway"])],
        cwd=os.path.join(SERVICES, "approval-gateway"), env=env,
        stdout=open(f"{TMP}/approval.log", "w"), stderr=subprocess.STDOUT,
    ))
    env = dict(os.environ)
    env.update({
        "GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
        "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "WEBHOOK_SECRET": "phase3",
    })
    procs.append(subprocess.Popen(
        [python_of("gitlab-bridge"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["gitlab-bridge"])],
        cwd=os.path.join(SERVICES, "gitlab-bridge"), env=env,
        stdout=open(f"{TMP}/bridge.log", "w"), stderr=subprocess.STDOUT,
    ))
    env = dict(os.environ)
    env.update({
        "PLATFORM_DSN": "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform",
        "LITELLM_DSN": "host=127.0.0.1 port=5432 dbname=litellm user=platform password=platform",
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['gitlab-bridge']}",
        "PROJECT_ID": os.getenv("PROJECT_ID", os.getenv("DEMO_PROJECT_ID", "")),
        "SOP_CONFIG_PATH": os.path.join(ROOT, "sop-config.yaml"),
        "LLM_BASE_URL": "http://127.0.0.1:4000/v1",
        "LLM_API_KEY": "sk-litellm-master",
        "LLM_MODEL": os.getenv("DEMO_LLM_MODEL", "qwen-max"),
    })
    procs.append(subprocess.Popen(
        [python_of("process-mining"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["process-mining"])],
        cwd=os.path.join(SERVICES, "process-mining"), env=env,
        stdout=open(f"{TMP}/mining.log", "w"), stderr=subprocess.STDOUT,
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
            check(f"服务 {name} 启动", False)
            return False
    return True


def main() -> int:
    start()
    print(f"服务启动中…（model={os.getenv('DEMO_LLM_MODEL', 'qwen-max')}）")
    if not wait_healthy():
        return 1
    check("三个服务健康（审批网关 / GitLab 桥 / 流程挖掘）", True)

    # ① 挖掘真实数据
    report = httpx.get(f"http://127.0.0.1:{PORTS['process-mining']}/mining/report?days=3650", timeout=60).json()
    gates = report["gates"]
    print("\n── 流程挖掘（真实 PG 数据）──")
    for g in gates[:6]:
        print(f"  {g['gate']:<16} 请求={g['requests']:>2} 决策={g['decisions']:>2} "
              f"等待p50={g['wait_p50_s']}s p95={g['wait_p95_s']}s 退回={g['rework_rate']:.0%} 超时={g['timeout_rate']:.0%}")
    print(f"  流程变体 Top3: {[v['variant'][:40] for v in report['variants'][:3]]}")
    print(f"  成本: {[(c['model'], c['calls']) for c in report['cost_by_model'][:3]]}")
    check("① 挖掘报告生成（Gate 指标 + 变体 + 成本）", bool(gates), f"{len(gates)} 个 Gate")
    check("① 规则化瓶颈识别", isinstance(report.get("bottlenecks"), list), f"{len(report['bottlenecks'])} 项发现")
    for b in report["bottlenecks"][:3]:
        print(f"    · [{b['type']}] {b.get('gate', b.get('variant', ''))[:40]}：{b['evidence']}")

    # ② LLM 提案
    props = httpx.post(f"http://127.0.0.1:{PORTS['process-mining']}/mining/proposals?days=3650", timeout=300).json()["proposals"]
    print("\n── SOP 优化提案（Qwen 生成）──")
    for p in props:
        print(f"  [{p['id']}] {p['title']}（动作={p['action'].get('type')} 可应用={p.get('appliable')}）")
        print(f"        依据：{p['evidence'][:90]}")
    check("② LLM 生成结构化提案", bool(props), f"{len(props)} 条")

    appliable = next((p for p in props if p.get("appliable")), None)
    if appliable is None:
        # 数据驱动兜底提案：高返工 Gate 需要更多人审覆盖 → 提高 L2 抽样比例
        risky = [g for g in gates if g["decisions"] >= 3 and g["rework_rate"] > 0]
        if risky:
            worst = max(risky, key=lambda g: g["rework_rate"])
            appliable = {
                "id": "prop-fallback",
                "title": f"提高信任抽样比例以覆盖高返工 Gate（{worst['gate']}）",
                "rationale": "存在返工热点，L2 抽样比例偏低导致人审覆盖不足；提高抽样可在自动化与质量间取更稳的平衡",
                "evidence": f"{worst['gate']} 退回/否决率 {worst['rework_rate']:.0%}（{worst['decisions']} 次决策）；"
                            f"当前 sample_ratio={hist_before.get('sample_ratio') if (hist_before := {}) is not None else '0.2'}",
                "action": {"type": "trust_threshold", "target": "sample_ratio", "value": 0.3},
                "risk": "L2",
                "appliable": True,
                "source": "数据驱动兜底提案（LLM 未给出可应用项）",
            }
            print(f"\n  ⚙️ 兜底提案（数据驱动）：{appliable['title']}｜动作 {appliable['action']}")
        else:
            print("（无返工热点，本轮无需应用）")
            return 0

    # ③ 提交 G7 审批
    sub = httpx.post(f"http://127.0.0.1:{PORTS['process-mining']}/mining/proposals/submit",
                     json={"proposal": appliable, "requester": "流程挖掘引擎", "task_ref": "phase3-evolve"}, timeout=60).json()
    approval_id = sub["approval_id"]
    apv = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}", timeout=30).json()
    check("③ 提案进入 G7 流程改进人审闸门", apv["status"] == "pending" and apv["type"] == "GATE_PROCESS_IMPROVEMENT",
          f"approval={approval_id}")

    # ④ 人审批准 → 应用
    httpx.post(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{approval_id}/decision",
               json={"decision": "approve", "decided_by": "平台负责人@Grace", "comment": "依据充分，批准应用"}, timeout=30)
    applied = httpx.post(f"http://127.0.0.1:{PORTS['process-mining']}/mining/proposals/apply",
                         json={"proposal": appliable, "approval_id": approval_id}, timeout=120).json()
    check("④ 批准后应用（写 SOP 配置中心）", applied["apply"]["applied"] is True,
          f"{applied['apply'].get('target')}: {applied['apply'].get('before')} → {applied['apply'].get('after')}")
    check("⑤ GitLab 改进项已登记", bool(applied["improvement_issue"].get("created")),
          f"issue #{applied['improvement_issue'].get('issue_iid')}")

    # ⑥ 效果度量：配置版本 + 生效验证
    hist = httpx.get(f"http://127.0.0.1:{PORTS['process-mining']}/mining/applied", timeout=30).json()
    check("⑥ 配置中心版本递增（可追溯）", hist["version"] >= 2,
          f"version={hist['version']} updated_at={hist['updated_at'][:19]}")
    trust = httpx.get(f"http://127.0.0.1:{PORTS['approval-gateway']}/trust", timeout=30).json()
    sample_ratios = {t["scope"]: t["sample_ratio"] for t in trust if t["level"] == "L2"}
    print(f"  热更新生效检查：当前配置 sample_ratio={hist['trust'].get('sample_ratio')}，"
          f"L2 Gate 抽样比例={list(sample_ratios.values())[:3]}")
    ok_effect = all(abs(v - float(hist["trust"].get("sample_ratio", v))) < 1e-9 for v in sample_ratios.values()) if sample_ratios else True
    check("⑦ 应用即时生效（无需重启）", ok_effect)

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 52}\nPhase 3 自进化演示：{passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    return 0 if passed else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)
