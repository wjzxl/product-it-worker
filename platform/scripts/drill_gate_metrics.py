#!/usr/bin/env python3
"""闸门度量管线的真实演练（M1）：证明 §8 的指标能被算出来。

场景设计（同一 task_ref 走完整链路，才能配对"被拦 → 补齐通过"的时长）：
  E1 被拦 → 补齐 impact_scope → E1 通过
  E2 被拦（前端未定义）→ 补齐 → E2 通过
  E3 被拦（证据缺失）→ 上传证据 → E3 通过
随后用 scripts/gate_metrics.py 的采集逻辑统计：拦截率、缺口分布、补齐时长。

注意：本演练产生的是**演练样本**，用于验证度量管线；生产指标须待真实需求跑过闸门后再统计
（见 reports/18 号复盘文档）。

用法（用 approval-gateway 的 venv：同时具备 httpx 与 sqlalchemy）：
    cd platform
    services/approval-gateway/.venv/bin/python scripts/drill_gate_metrics.py
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
TMP = tempfile.mkdtemp(prefix="m1-metrics-")
PORT = 8051
REPORTS = ROOT / "reports"
DSN = f"sqlite:///{TMP}/gateway.db"
TENANT = "m1"
TASK = "m1-metrics-task"


def python_of(service: str) -> str:
    return str(SERVICES / service / ".venv" / "bin" / "python")


def start_gateway() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"DATABASE_URL": DSN, "DEFAULT_TENANT": TENANT, "EVIDENCE_DIR": f"{TMP}/evidence"})
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


DESIGN_MISSING_FRONTEND = {"report": {f: {"x": "y"} for f in ("data", "business", "api", "test")}}
DESIGN_FULL = {"report": {f: {"x": "y"} for f in ("data", "business", "api", "frontend", "test")}}
UX = {"empty": "空态", "error": "报错", "loading": "加载"}


def main() -> int:
    proc = start_gateway()
    base = f"http://127.0.0.1:{PORT}"
    hdr = {"X-Tenant": TENANT}
    steps: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    try:
        if not wait_healthy():
            print("❌ 网关未就绪", file=sys.stderr)
            return 1

        # E1：被拦 → 补齐 → 通过（同一 task_ref，可配对补齐时长）
        e1_ctx = {"prd_ref": "PRD v1", "sources": [{"kind": "chat", "ref": "feishu://x", "quote": "加导出"}],
                  "change_targets": ["src/report/export.py"], "rollback_plan": "一键切回"}
        e1_bad = httpx.post(f"{base}/eng-gates/E1/check", json={"task_ref": TASK, "context": e1_ctx}, headers=hdr, timeout=30).json()
        step("E1 被拦（缺影响范围）", e1_bad["blocked"] is True, f"gaps={[g['id'] for g in e1_bad['gaps']]}")
        time.sleep(1.2)
        e1_ok = httpx.post(f"{base}/eng-gates/E1/check", headers=hdr, timeout=30,
                           json={"task_ref": TASK, "context": {**e1_ctx, "impact_scope": "仅新增导出接口，不改表结构"}}).json()
        step("E1 补齐后通过", e1_ok["passed"] is True)

        # E2：被拦 → 补齐 → 通过
        e2_bad = httpx.post(f"{base}/eng-gates/E2/check", headers=hdr, timeout=30, json={"task_ref": TASK, "context": {
            "design_docs": DESIGN_MISSING_FRONTEND, "ux_states": UX,
            "api_contract_ref": "a", "data_dictionary_ref": "b"}}).json()
        step("E2 被拦（前端设计缺失）", e2_bad["blocked"] is True, f"gaps={[g['id'] for g in e2_bad['gaps']]}")
        time.sleep(1.2)
        e2_ok = httpx.post(f"{base}/eng-gates/E2/check", headers=hdr, timeout=30, json={"task_ref": TASK, "context": {
            "design_docs": DESIGN_FULL, "ux_states": UX,
            "api_contract_ref": "a", "data_dictionary_ref": "b"}}).json()
        step("E2 补齐后通过", e2_ok["passed"] is True)

        # E3：被拦（证据缺失）→ 上传证据 → 通过
        items = [{"id": "acc-1"}, {"id": "acc-2"}]
        e3_ctx = {"artifacts": ["src/report/export.py"], "acceptance_items": items,
                  "plan_consistency": "一致", "handover": "README"}
        e3_bad = httpx.post(f"{base}/eng-gates/E3/check", json={"task_ref": TASK, "context": e3_ctx}, headers=hdr, timeout=30).json()
        step("E3 被拦（证据缺失）", e3_bad["blocked"] is True, f"gaps={[g['id'] for g in e3_bad['gaps']]}")
        time.sleep(1.2)
        for item in ("acc-1", "acc-2"):
            httpx.post(f"{base}/evidence", headers=hdr, timeout=30,
                       data={"task_ref": TASK, "acceptance_item": item, "kind": "export", "note": "M1 度量演练"},
                       files={"file": (f"{item}.csv", b"a,b\n1,2\n", "text/csv")})
        e3_ok = httpx.post(f"{base}/eng-gates/E3/check", json={"task_ref": TASK, "context": e3_ctx}, headers=hdr, timeout=30).json()
        step("E3 补齐证据后通过", e3_ok["passed"] is True)

        # 度量：直接调用采集逻辑（同一进程，读同一个 sqlite）
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib

        gate_metrics = importlib.import_module("gate_metrics")
        data = gate_metrics.collect(DSN)
        rendered = gate_metrics.render(data)
        print("\n" + rendered + "\n")

        ok = all(s[1] for s in steps) and data["checks_total"] == 6
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"m1-gate-metrics-drill-{stamp}.md"
        report.write_text(f"""# M1 闸门度量演练（度量管线验证）

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 场景：同一任务（`{TASK}`）走完 E1 → E2 → E3，各自"被拦一次、补齐后通过"
- 数据源：临时库（演练样本，**不代表生产指标**）

## 步骤结果

| 步骤 | 结果 | 说明 |
|---|---|---|
""" + "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in steps) + f"""

## 度量输出（scripts/gate_metrics.py 采集）

```
{rendered}
```

## 结论

{'度量管线可用：拦截率、缺口分布、补齐时长均能算出；误拦率需人工反馈字段（待 WP10 驾驶舱补标注入口）。' if ok else '度量管线存在问题，见上表。'}
""", encoding="utf-8")
        print(f"报告：{report.relative_to(ROOT)}")
        print(f"结论：{'度量管线验证通过 🎉' if ok else '存在问题 ❌'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
