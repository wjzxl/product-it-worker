"""度量面板 + 决策驾驶舱（WP10）。

- `GET /metrics`：三域指标（只读）
- `GET /console`：**待决策队列**（待审审批 + 闸门阻断 + 等待中的运行，含深链）
- `POST /console/decide`、`POST /console/feedback`：从驾驶舱直接决策/标记误拦
  （都转调审批网关，**不绕过审计留痕**）
"""
from __future__ import annotations

import logging

import html as _html

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import console
from .metrics import collect
from .service_auth import install_service_auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="metrics-dashboard",
    description="三域（研发/测试/运维）指标面板：Gate 决策、任务、LLM 成本（设计文档 05 度量体系）",
    version="0.1.0",
)
install_service_auth(app, "metrics-dashboard")


@app.get("/metrics")
def metrics() -> dict:
    return collect()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "metrics-dashboard", "version": "0.1.0"}


class DecideIn(BaseModel):
    approval_id: str
    decision: str
    comment: str = ""
    decided_by: str = "驾驶舱"


class FeedbackIn(BaseModel):
    check_id: str
    verdict: str
    note: str = ""
    by: str = "驾驶舱"


@app.get("/console")
def decision_console(tenant: str = "default") -> dict:
    """待决策队列（WP10）：每条都带"为什么卡住 + 可执行动作 + 深链"。"""
    return console.build_queue(tenant=tenant)


@app.post("/console/decide")
def console_decide(body: DecideIn, request: Request) -> dict:
    """从驾驶舱提交人审决策（转调审批网关，保留三要素与审计）。

    鉴权专项：**透传调用方身份**——否则 oidc 模式下网关 401（驾驶舱永远点不动），
    放行模式下网关也无从记录"是谁批的"。
    """
    if body.decision not in ("approve", "reject", "revise"):
        raise HTTPException(status_code=422, detail="decision 必须为 approve/reject/revise")
    try:
        return console.submit_decision(body.approval_id, body.decision, body.comment, body.decided_by,
                                       headers=console.forward_headers(request.headers))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"审批网关决策失败：{type(exc).__name__}: {exc}") from exc


@app.post("/console/feedback")
def console_feedback(body: FeedbackIn, request: Request) -> dict:
    """误拦反馈（M1 决策 D3）：标记"其实该放行"，用于计算误拦率。（同样透传身份头）"""
    if body.verdict not in ("valid", "false_positive"):
        raise HTTPException(status_code=422, detail="verdict 必须为 valid/false_positive")
    try:
        return console.submit_feedback(body.check_id, body.verdict, body.note, body.by,
                                       headers=console.forward_headers(request.headers))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"反馈提交失败：{type(exc).__name__}: {exc}") from exc


def esc(value) -> str:
    """HTML 转义（安全专项）：驾驶舱里的标题/需求文本/阶段名来自用户与上游服务，
    直接拼进 HTML 就是存储型 XSS。所有插值一律先过这里。"""
    return _html.escape(str(value if value is not None else ""), quote=True)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    d = collect()
    gate_rows = "".join(
        f"<tr><td>{esc(g['gate'])}</td><td>{esc(g['domain'])}</td><td>{g['count']}</td>"
        f"<td>{g['avg_decision_s'] if g['avg_decision_s'] is not None else '-'}</td></tr>"
        for g in d["gates"]
    )
    task_rows = "".join(
        f"<tr><td>{esc(t['domain'])}</td><td>{t['count']}</td><td>{t['done']}</td></tr>"
        for t in d["tasks_by_sop"]
    )
    cost_rows = "".join(
        f"<tr><td>{esc(m['model'])}</td><td>{m['calls']}</td><td>{m['spend_usd']:.4f}</td><td>{m['tokens']}</td></tr>"
        for m in d.get("spend_by_model", [])
    )
    # WP10 待决策队列（课程图 67：驾驶舱要显示"卡在哪里、什么需要自己决定"）
    queue = console.build_queue()
    if queue["queue"]:
        rows = []
        for item in queue["queue"][:20]:
            actions = []
            if item["kind"] == "approval":
                for act, label in (("approve", "批准"), ("reject", "否决"), ("revise", "退回")):
                    actions.append(
                        f"<button onclick=\"decide('{esc(item['id'])}','{act}')\">{label}</button>"
                    )
            elif item["kind"] == "gate_block":
                actions.append(f"<button onclick=\"feedback('{esc(item['id'])}','valid')\">确实该拦</button>")
                actions.append(f"<button onclick=\"feedback('{esc(item['id'])}','false_positive')\">其实可放行</button>")
            rows.append(
                f"<tr><td>{esc(item['kind'])}</td><td>{esc(item['title'])}</td>"
                f"<td>{esc(item['what'][:60])}</td>"
                f"<td>{esc(item['why_waiting'][:40])}</td><td>{esc(item.get('task_ref', ''))}</td>"
                f"<td>{' '.join(actions) or '-'}</td></tr>"
            )
        queue_rows = "".join(rows)
    else:
        queue_rows = "<tr><td colspan='6'>当前没有待决策事项</td></tr>"
    queue_note = (
        f"上游不可用：{esc(', '.join(queue['upstream_errors']))}" if queue["upstream_errors"] else "上游全部可用"
    )
    fb = queue.get("feedback_stats") or {}
    fb_note = (
        f"误拦率 {fb['false_positive_rate']:.1%}（已反馈 {fb['judged']}/{fb['blocked_total']}）"
        if fb.get("false_positive_rate") is not None
        else f"误拦率待反馈（阻断 {fb.get('blocked_total', 0)} 次，已反馈 {fb.get('judged', 0)} 次）"
    )

    decisions = esc("，".join(f"{k}: {v}" for k, v in d.get("decision_dist", {}).items()) or "-")
    statuses = esc("，".join(f"{k}: {v}" for k, v in d.get("status_dist", {}).items()) or "-")
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
 button{{margin-right:4px;padding:3px 8px;border:1px solid #d0d3d9;background:#fff;border-radius:4px;cursor:pointer;font-size:12px}}
 button:hover{{background:#f0f2f5}}
</style></head><body>
<h1>平台度量面板 · 四域 + 平台域指标</h1>
<div class="cards">
 <div class="card"><div class="v">{d['approvals_total']}</div><div class="k">审批单总数</div></div>
 <div class="card"><div class="v">{avg}</div><div class="k">人审平均决策耗时</div></div>
 <div class="card"><div class="v">{d['tasks_total']}</div><div class="k">任务总数</div></div>
 <div class="card"><div class="v">{d['events_total']}</div><div class="k">时间线事件</div></div>
 <div class="card"><div class="v">{spend}</div><div class="k">LLM 累计成本</div></div>
</div>
<h2>待决策队列（驾驶舱）</h2>
<table><tr><th>类型</th><th>事项</th><th>做了什么/缺口</th><th>为什么卡住</th><th>任务</th><th>动作</th></tr>{queue_rows}</table>
<div class="muted">{queue_note}｜{fb_note}</div>
<script>
async function decide(id, decision) {{
  const comment = prompt('决策理由（否决/退回必填）', '');
  const r = await fetch('/console/decide', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{approval_id:id, decision:decision, comment:comment||'', decided_by:'驾驶舱'}})}});
  alert(r.ok ? '已提交：' + decision : '提交失败：' + await r.text());
  if (r.ok) location.reload();
}}
async function feedback(id, verdict) {{
  const note = prompt('说明（可留空）', '');
  const r = await fetch('/console/feedback', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{check_id:id, verdict:verdict, note:note||'', by:'驾驶舱'}})}});
  alert(r.ok ? '已记录反馈' : '提交失败：' + await r.text());
  if (r.ok) location.reload();
}}
</script>
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
