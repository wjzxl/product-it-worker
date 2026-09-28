/* 平台审批台脚本。
 *
 * 安全约定（与 index.html 的 CSP 配套，别改回去）：
 *   ① **所有**来自接口的字符串都过 esc() 再进 DOM —— 阶段名/需求文本/证据/AI 自审
 *      都可能是用户或模型输入，直接拼进 innerHTML 就是存储型 XSS；
 *   ② 不使用内联事件处理器（onclick=""），改用 data-* + 事件委托 ——
 *      这样 CSP 才能用 `script-src 'self'`（内联处理器会被浏览器直接拒绝执行）。
 */
"use strict";

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

let current = null;

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.hidden = false;
  setTimeout(() => { t.hidden = true; }, 2500);
}

async function loadPosture() {
  // 治理姿态：让审批人知道当前闸门是严是松（PLATFORM_ENV=dev 时鉴权/闸门是松的）
  try {
    const h = await (await fetch("/health")).json();
    const sec = h.security || {};
    if (sec.compliant === false && (sec.warnings || []).length) {
      const badge = document.getElementById("posture");
      badge.textContent = `⚠ ${sec.platform_env || "dev"} 档位：${sec.warnings.length} 项治理开关未开（仅限内部试用）`;
      badge.hidden = false;
    }
  } catch (e) { /* 姿态拿不到不影响审批本身 */ }
}

async function loadList(selectId) {
  const res = await fetch("/approvals?status=pending");
  const items = await res.json();
  document.getElementById("count").textContent = items.length;
  const list = document.getElementById("list");
  if (!items.length) { list.innerHTML = '<div class="empty">暂无待审批事项 🎉</div>'; return; }
  list.innerHTML = items.map(a => {
    const risk = (a.context_snapshot || {}).risk || {};
    return `<div class="row ${selectId === a.id ? "active" : ""}" data-id="${esc(a.id)}">
        <div class="stage">${esc(a.stage)} <span class="id">#${esc(a.id)}</span></div>
        <div class="meta">风险：${esc(risk.level)}（${esc(risk.score)}分）· 截止 ${esc(new Date(a.deadline).toLocaleString())}</div>
      </div>`;
  }).join("");
  if (selectId) openDetail(selectId);
}

async function openDetail(id) {
  const res = await fetch("/approvals/" + encodeURIComponent(id));
  if (!res.ok) { toast("审批单不存在或已处理"); return; }
  current = await res.json();
  document.querySelectorAll(".row").forEach(r => {
    r.classList.toggle("active", r.dataset.id === String(id));
  });
  const ctx = current.context_snapshot || {};
  const risk = ctx.risk || {};
  document.getElementById("detail").innerHTML = `
    <div class="field"><label>阶段 / 发起者</label><pre>${esc(current.stage)} · ${esc(current.requester)} · v${esc(current.version)}</pre></div>
    <div class="field"><label>① 做了什么</label><pre>${esc(ctx.diff_summary)}</pre></div>
    <div class="field"><label>② 依据</label><pre>${esc((ctx.evidence || []).join("\n"))}</pre></div>
    <div class="field"><label>③ 风险自评</label><pre>${esc(risk.level)}（${esc(risk.score)} 分）${esc((risk.factors || []).join("，"))}</pre></div>
    ${ctx.self_review ? `<div class="field"><label>AI 自审</label><pre>${esc(ctx.self_review)}</pre></div>` : ""}
    <div class="field"><label>产物</label><pre>${esc(ctx.artifact_ref)}</pre></div>
    <div class="actions">
      <button class="primary" data-action="approve">批准</button>
      <button class="warn" data-action="revise">退回修订</button>
      <button class="danger" data-action="reject">拒绝</button>
    </div>`;
}

async function decide(action, comment) {
  if (!current) return;
  if ((action === "revise" || action === "reject") && !String(comment || "").trim()) {
    toast("必须填写原因"); return;
  }
  const decidedBy = prompt("您的姓名/账号（留痕）：") || "审批人";
  const res = await fetch(`/approvals/${encodeURIComponent(current.id)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: action, decided_by: decidedBy, comment: comment || "" })
  });
  if (res.ok) { toast("已提交 ✓"); current = null; loadList(); }
  else {
    const err = await res.json().catch(() => ({}));
    toast("操作失败：" + (err.detail || res.status));
  }
}

/* 事件委托：CSP 下没有内联 onclick，所有点击都在这里分发 */
document.addEventListener("click", (ev) => {
  const row = ev.target.closest(".row");
  if (row && row.dataset.id) { openDetail(row.dataset.id); return; }
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "approve") { decide("approve", ""); return; }
  const label = action === "revise" ? "退回原因（必填，将进入反馈闭环）："
                                    : "拒绝原因（必填，将进入反馈闭环）：";
  const comment = prompt(label);
  if (comment !== null) decide(action, comment);
});

fetch("/health").then(r => r.json())
  .then(h => { document.getElementById("version").textContent = "v" + h.version; })
  .catch(() => {});
loadPosture();
const params = new URLSearchParams(location.search);
loadList(params.get("approval_id"));
