#!/usr/bin/env python3
"""WP5 证据库**真实演练**：从"证据不齐不能交付"到"独立复验通过"。

对应课程图 33：清单写具体操作和预期结果，并保存截图/日志/导出文件；需求、代码产物与测试证据
要互相对应；**半年后接手的人要能复验**，而不是相信一句"做完了"。

验收口径（17 号计划 WP5）：
  ① 上传导出文件/日志/截图三类证据，hash 与大小可核对；
  ② E3 闸门**从证据库自动绑定**：验收项缺证据 → 拒绝交付；
  ③ 补齐全部证据 → 放行；
  ④ **独立复验**：以"未参与者"身份（新会话、只看材料）重新下载全部证据并校验 hash，
     结论与原始一致；
  ⑤ 证据被篡改/丢失时必须被发现；
  ⑥ 跨产品线不可见。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python scripts/drill_evidence_store.py
"""
from __future__ import annotations

import hashlib
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
TMP = tempfile.mkdtemp(prefix="wp5-drill-")
PORT = 8051
REPORTS = ROOT / "reports"
TENANT = "drill"
TASK = "wp5-drill-task"

# 三类证据（对应课程图 33 的证据类型表）
EVIDENCES = [
    ("acc-cost-column", "export", "export-sample.csv",
     "warehouse,month,cost_amount\nWH-01,2026-09,1234.56\n".encode()),
    ("acc-permission", "log", "permission-denied.log",
     "2026-09-17T09:00:00Z WARN export denied user=ops reason=no export:user\n".encode()),
    ("acc-empty-state", "screenshot", "empty-state.png",
     b"\x89PNG\r\n\x1a\n" + b"fake-png-for-drill" * 8),
]


def python_of(service: str) -> str:
    return str(SERVICES / service / ".venv" / "bin" / "python")


def start_gateway() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{TMP}/gateway.db", "DEFAULT_TENANT": TENANT,
                "EVIDENCE_DIR": f"{TMP}/evidence"})
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


DESIGN = {"report": {f: {"x": "y"} for f in ("data", "business", "api", "frontend", "test")}}
UX = {"empty": "空态", "error": "报错", "loading": "加载"}


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
        check("证据库服务健康", True)

        # 前置：E1/E2 通过（E3 要求前序闸门已过）
        httpx.post(f"{base}/eng-gates/E1/check", headers=hdr, timeout=30, json={"task_ref": TASK, "context": {
            "prd_ref": "PRD v1", "sources": [{"kind": "voice", "ref": "feishu://x", "quote": "成本列运营不能看"}],
            "change_targets": ["src/report/export.py"], "impact_scope": "仅新增导出接口",
            "rollback_plan": "保留上一版本一键切回"}})
        httpx.post(f"{base}/eng-gates/E2/check", headers=hdr, timeout=30, json={"task_ref": TASK, "context": {
            "design_docs": DESIGN, "ux_states": UX, "api_contract_ref": "api-protocol.md",
            "data_dictionary_ref": "data-dictionary.md"}})

        items = [{"id": item} for item, _k, _f, _c in EVIDENCES]
        e3_ctx = {"artifacts": ["src/report/export.py", "docs/report-export.md"],
                  "acceptance_items": items, "plan_consistency": "与方案一致", "handover": "README + 回滚步骤"}

        # ① 证据未上传 → E3 拒绝
        before = httpx.post(f"{base}/eng-gates/E3/check", headers=hdr, timeout=30,
                            json={"task_ref": TASK, "context": e3_ctx}).json()
        gap = next((g for g in before["gaps"] if g["id"] == "acceptance_evidence"), {})
        check("① 证据未上传时 E3 拒绝交付", before["blocked"] is True and "acc-cost-column" in gap.get("detail", ""),
              gap.get("detail", ""))

        # ② 上传三类证据
        uploaded = []
        for item, kind, filename, content in EVIDENCES:
            r = httpx.post(f"{base}/evidence", headers=hdr, timeout=30,
                           data={"task_ref": TASK, "acceptance_item": item, "kind": kind, "note": "演练证据"},
                           files={"file": (filename, content, "application/octet-stream")})
            r.raise_for_status()
            meta = r.json()
            assert meta["sha256"] == hashlib.sha256(content).hexdigest(), "hash 应与本地计算一致"
            uploaded.append((meta, content))
        check("② 三类证据上传成功（导出文件/日志/截图）", len(uploaded) == 3,
              "，".join(f"{m['kind']}:{m['filename']}({m['size']}B)" for m, _ in uploaded))

        # ③ E3 自动绑定证据库 → 放行
        after = httpx.post(f"{base}/eng-gates/E3/check", headers=hdr, timeout=30,
                           json={"task_ref": TASK, "context": e3_ctx}).json()
        check("③ 证据齐备后 E3 放行（自动绑定，不需调用方自报）", after["passed"] is True,
              f"检查项 {len(after['results'])} 项全过")

        # ④ 独立复验：新会话、只凭元数据重新下载并核对 hash
        listing = httpx.get(f"{base}/evidence", headers=hdr, params={"task_ref": TASK}, timeout=30).json()
        reverified, mismatches = 0, []
        for meta in listing["evidence"]:
            resp = httpx.get(f"{base}{meta['download_url']}", headers=hdr, timeout=30)
            digest = hashlib.sha256(resp.content).hexdigest()
            if digest == meta["sha256"] and resp.headers.get("X-Evidence-Sha256") == meta["sha256"]:
                reverified += 1
            else:
                mismatches.append(meta["id"])
        check("④ 独立复验：重下载逐件核对 hash 一致", reverified == 3 and not mismatches,
              f"{reverified}/3 一致；复验接口：{httpx.get(f'{base}/evidence/verify', headers=hdr, params={'task_ref': TASK}, timeout=30).json()}")

        # ⑤ 篡改必须被发现
        target = listing["evidence"][0]
        artifacts = list(Path(f"{TMP}/evidence").rglob(f"{target['id']}-*"))
        if artifacts:
            artifacts[0].write_bytes(b"tampered")
        verify = httpx.get(f"{base}/evidence/verify", headers=hdr, params={"task_ref": TASK}, timeout=30).json()
        check("⑤ 证据被篡改会被复验发现", len(verify["broken"]) == 1 and verify["verified"] == 2,
              f"broken={verify['broken'][0]['error'][:60] if verify['broken'] else '-'}")

        # ⑥ 跨产品线不可见
        cross = httpx.get(f"{base}/evidence/{target['id']}/content", headers={"X-Tenant": "line-b"}, timeout=30)
        check("⑥ 跨产品线不可见（租户隔离）", cross.status_code == 404, f"HTTP {cross.status_code}")

        ok = all(c[1] for c in checks)
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = REPORTS / f"wp5-evidence-drill-{stamp}.md"
        rows = "\n".join(f"| {n} | {'✅' if o else '❌'} | {d} |" for n, o, d in checks)
        report.write_text(f"""# WP5 证据库与验收绑定演练报告

- 执行时间：{datetime.now(timezone.utc).isoformat()}
- 任务：`{TASK}`｜产品线：`{TENANT}`
- 对应课程：图 33（验收必须可判定、也必须有证据）

## 验收结果（{sum(1 for c in checks if c[1])}/{len(checks)}）

| 验收项 | 结果 | 证据 |
|---|---|---|
{rows}

## 证据清单（可复验）

| 验收项 | 类型 | 文件 | sha256（前 16 位） |
|---|---|---|---|
""" + "\n".join(
            f"| {item} | {kind} | {filename} | {hashlib.sha256(content).hexdigest()[:16]} |"
            for (item, kind, filename, content) in EVIDENCES
        ) + f"""

## 结论

{'全部通过：证据与验收项绑定、可独立复验、篡改可发现、租户隔离生效。' if ok else '存在未通过项，见上表。'}
""", encoding="utf-8")
        print(f"\n报告：{report.relative_to(ROOT)}")
        print(f"结论：{'全部通过 🎉' if ok else '存在失败项 ❌'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
