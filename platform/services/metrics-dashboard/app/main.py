"""三域指标度量面板：GET /metrics（JSON）+ GET /（HTML 面板）。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .metrics import collect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="metrics-dashboard",
    description="三域（研发/测试/运维）指标面板：Gate 决策、任务、LLM 成本（设计文档 05 度量体系）",
    version="0.1.0",
)


@app.get("/metrics")
def metrics() -> dict:
    return collect()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "metrics-dashboard", "version": "0.1.0"}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    d = collect()
    gate_rows = "".join(
        f"<tr><td>{g['gate']}</td><td>{g['domain']}</td><td>{g['count']}</td>"
        f"<td>{g['avg_decision_s'] if g['avg_decision_s'] is not None else '-'}</td></tr>"
        for g in d["gates"]
    )
    task_rows = "".join(
        f"<tr><td>{t['domain']}</td><td>{t['count']}</td><td>{t['done']}</td></tr>" for t in d["tasks_by_sop"]
    )
    cost_rows = "".join(
        f"<tr><td>{m['model']}</td><td>{m['calls']}</td><td>{m['spend_usd']:.4f}</td><td>{m['tokens']}</td></tr>"
        for m in d.get("spend_by_model", [])
    )
    decisions = "，".join(f"{k}: {v}" for k, v in d.get("decision_dist", {}).items()) or "-"
    statuses = "，".join(f"{k}: {v}" for k, v in d.get("status_dist", {}).items()) or "-"
    spend = f"${d['spend_total_usd']:.4f}" if d.get("spend_total_usd") is not None else "-"
    avg = f"{d['decision_avg_s']:.1f}s" if d.get("decision_avg_s") else "-"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta http-equiv="refresh" content="30">
<title>平台度量面板 · Phase 2</title>
<style>
 body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f6f8;color:#1f2329;margin:0;padding:24px}}
 h1{{font-size:20px}} h2{{font-size:15px;margin-top:24px}}
 .cards{{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}}
 .card{{background:#fff;border:1px solid #e3e5e8;border-radius:8px;padding:16px 20px;min-width:160px}}
 .card .v{{font-size:24px;font-weight:600}} .card .k{{color:#8a919f;font-size:12px;margin-top:4px}}
 table{{background:#fff;border-collapse:collapse;width:100%;max-width:760px;border:1px solid #e3e5e8;border-radius:8px;overflow:hidden}}
 th,td{{padding:8px 12px;border-bottom:1px solid #eef0f2;font-size:13px;text-align:left}}
 th{{background:#fafbfc;color:#5c6470;font-weight:500}}
 .muted{{color:#8a919f;font-size:12px;margin-top:8px}}
</style></head><body>
<h1>平台度量面板 · 四域 + 平台域指标</h1>
<div class="cards">
 <div class="card"><div class="v">{d['approvals_total']}</div><div class="k">审批单总数</div></div>
 <div class="card"><div class="v">{avg}</div><div class="k">人审平均决策耗时</div></div>
 <div class="card"><div class="v">{d['tasks_total']}</div><div class="k">任务总数</div></div>
 <div class="card"><div class="v">{d['events_total']}</div><div class="k">时间线事件</div></div>
 <div class="card"><div class="v">{spend}</div><div class="k">LLM 累计成本</div></div>
</div>
<h2>Gate 决策（G1–G7，含平台域 G7 流程改进闸门）</h2>
<table><tr><th>Gate</th><th>域</th><th>审批数</th><th>平均决策(s)</th></tr>{gate_rows}</table>
<h2>任务（按域）</h2>
<table><tr><th>域</th><th>任务数</th><th>完成</th></tr>{task_rows}</table>
<h2>审批决策 / 状态</h2>
<table><tr><th>决策分布</th><th>状态分布</th></tr><tr><td>{decisions}</td><td>{statuses}</td></tr></table>
<h2>LLM 成本（LiteLLM 计量）</h2>
<table><tr><th>模型</th><th>调用数</th><th>花费($)</th><th>tokens</th></tr>{cost_rows}</table>
<div class="muted">数据源：PostgreSQL（平台库 + LiteLLM 库）｜每 30s 自动刷新｜生成时间 {d['generated_at'][:19]}Z</div>
</body></html>"""
