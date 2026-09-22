#!/usr/bin/env bash
# Probes for the 产品研发智能化平台 test campaign.
# Each subcommand prints one final line: either "RESULT: PASS <what>" or
# "FINDING: ..." and exits 1 when the product violates the requirement.
# Exit 2 means the probe itself was invalid (setup failed) — that is a harness
# error, never a product finding.
set -uo pipefail

GW=${GW:-http://127.0.0.1:18001}
TC=${TC:-http://127.0.0.1:18004}
RT=${RT:-http://127.0.0.1:18002}
D=/tmp/pst-sut
mkdir -p "$D"
J() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }

# 鉴权专项后 SUT 默认以 SERVICE_AUTH_MODE=token 运行：所有请求统一带上服务令牌。
# 用函数包装而不是逐个 curl 加参数——漏一处就会得到一个"看似 401 其实是探针写错"的假结论。
SERVICE_TOKEN=${SERVICE_AUTH_TOKEN:-pst-sut-token}
curl() { command curl -H "X-Service-Token: ${SERVICE_TOKEN}" "$@"; }

# A valid approval body. RiskSnapshot requires score (0-100), level and factors.
# Named arguments keep the call sites readable and prevent positional mistakes.
approval_body() { # --ref --gate --level --options --policy  (all optional but --ref)
  local ref="" gate="GATE_REQUIREMENT_REVIEW" level="L2" options='["approve","reject","revise"]' policy='{}'
  while [ $# -gt 0 ]; do
    case "$1" in
      --ref) ref="$2"; shift 2 ;;
      --gate) gate="$2"; shift 2 ;;
      --level) level="$2"; shift 2 ;;
      --options) options="$2"; shift 2 ;;
      --policy) policy="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  local score=20
  [ "$level" = "L1" ] && score=5
  local deadline
  deadline=$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=72)).isoformat())')
  printf '{"type":"%s","stage":"probe","requester":"campaign","task_ref":"%s","context_snapshot":{"artifact_ref":"%s","diff_summary":"probe work","evidence":["http://evidence/1"],"risk":{"score":%s,"level":"%s","factors":["probe"]},"self_review":"probe"},"options":%s,"deadline":"%s","escalation_policy":%s}' \
    "$gate" "$ref" "$ref" "$score" "$level" "$options" "$deadline" "$policy"
}

mk_approval() { # $1=ref $2=tenant $3=level -> full response JSON on stdout
  curl -s -X POST "$GW/approvals" -H 'Content-Type: application/json' -H "X-Tenant: $2" \
    -d "$(approval_body --ref "$1" --level "${3:-L2}")"
}

# ---------------------------------------------------------------- P0 proofs

# SEC-001: an unauthenticated, unsigned IM card callback must not decide a gate.
probe_im_callback() {
  local id resp code status
  id=$(mk_approval "SEC-001" victim L2 | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create approval"; return 2; }
  code=$(curl -s -o "$D/im.json" -w '%{http_code}' -X POST "$GW/callbacks/im/card" \
    -H 'Content-Type: application/json' \
    -d "{\"approval_id\":\"$id\",\"action\":\"approve\",\"operator\":\"attacker\",\"comment\":\"\"}")
  status=$(curl -s -H 'X-Tenant: victim' "$GW/approvals/$id" | J "['status']")
  echo "callback_http=$code status_after=$status (no signature, no auth, tenant header absent)"
  if [ "$code" = "200" ] && [ "$status" = "approved" ]; then
    echo "FINDING: unauthenticated unsigned IM callback approved a human gate"; return 1
  fi
  echo "RESULT: PASS IM callback cannot decide a gate without authentication"
}

# SEC-002: reading another tenant's approval must fail.
probe_cross_tenant_read() {
  local id code
  id=$(mk_approval "SEC-002" tenantA L2 | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create approval"; return 2; }
  code=$(curl -s -o "$D/x.json" -w '%{http_code}' -H 'X-Tenant: tenantB' "$GW/approvals/$id")
  echo "created=$id as tenantA; read as tenantB http=$code"
  if [ "$code" = "200" ]; then
    echo "FINDING: tenantB read tenantA's approval (tenant is a client-asserted header)"; return 1
  fi
  echo "RESULT: PASS cross-tenant read rejected"
}

# SEC-003: resubmit must be tenant-scoped.
probe_resubmit_tenant() {
  local id code before after
  id=$(mk_approval "SEC-003" tenantA L2 | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create approval"; return 2; }
  curl -s -o /dev/null -X POST "$GW/approvals/$id/decision" -H 'Content-Type: application/json' \
    -H 'X-Tenant: tenantA' -d '{"decision":"revise","decided_by":"tenantA.reviewer","comment":"rework"}'
  before=$(curl -s -H 'X-Tenant: tenantA' "$GW/approvals/$id" | J "['status']")
  # resubmit takes a whole ApprovalRequestCreate body; the point of the probe is
  # that router.py:295 omits the tenant filter that every sibling route applies.
  code=$(curl -s -o "$D/rs.json" -w '%{http_code}' -X POST "$GW/approvals/$id/resubmit" \
    -H 'Content-Type: application/json' -H 'X-Tenant: tenantB' \
    -d "$(approval_body --ref SEC-003)")
  after=$(curl -s -H 'X-Tenant: tenantA' "$GW/approvals/$id" | J "['status']")
  echo "before=$before resubmit_as_tenantB_http=$code after=$after"
  if [ "$after" != "$before" ] && [ "$code" = "200" ]; then
    echo "FINDING: tenantB mutated tenantA's approval through resubmit (tenant filter missing)"; return 1
  fi
  echo "RESULT: PASS resubmit honours tenant scope"
}

# SEC-004: a caller must not be able to switch off a blocking engineering gate.
probe_gate_mode_override() {
  local b o blocked blocked_off
  curl -s -o "$D/e1.json" -w '%{http_code}' -X POST "$GW/eng-gates/E1/check" \
    -H 'Content-Type: application/json' -H 'X-Tenant: default' \
    -d '{"task_ref":"SEC-004","mode":"block","persist":true,"context":{}}' >/dev/null
  blocked=$(J "['blocked']" < "$D/e1.json")
  curl -s -o "$D/e1off.json" -w '%{http_code}' -X POST "$GW/eng-gates/E1/check" \
    -H 'Content-Type: application/json' -H 'X-Tenant: default' \
    -d '{"task_ref":"SEC-004","mode":"off","persist":true,"context":{}}' >/dev/null
  blocked_off=$(J "['blocked']" < "$D/e1off.json")
  echo "with mode=block the gate blocked=$blocked; caller-supplied mode=off blocked=$blocked_off"
  if [ "$blocked" = "True" ] && [ "$blocked_off" = "False" ]; then
    echo "FINDING: the caller overrode the rule's own mode and disabled a blocking gate"; return 1
  fi
  echo "RESULT: PASS rule mode is not caller-overridable"
}

# GATE-001: every gate check must leave an audit record.
probe_gate_persist() {
  local before after
  before=$(curl -s -H 'X-Tenant: default' "$GW/eng-gates/checks?task_ref=GATE-001&limit=100" | J "['__len__']()" 2>/dev/null)
  [ -z "$before" ] && before=$(curl -s -H 'X-Tenant: default' "$GW/eng-gates/checks?task_ref=GATE-001&limit=100" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
  curl -s -o /dev/null -X POST "$GW/eng-gates/E1/check" -H 'Content-Type: application/json' \
    -H 'X-Tenant: default' -d '{"task_ref":"GATE-001","persist":false,"context":{}}'
  after=$(curl -s -H 'X-Tenant: default' "$GW/eng-gates/checks?task_ref=GATE-001&limit=100" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
  echo "audit rows before=$before after_a_persist_false_check=$after"
  if [ "$after" = "$before" ]; then
    echo "FINDING: persist=false suppressed the audit record for a gate check"; return 1
  fi
  echo "RESULT: PASS every gate check is persisted"
}

# GATE-002: a self-declared risk level must not produce a timeout auto-approval.
probe_timeout_autoapprove() {
  local past id status
  past=$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=2)).isoformat())')
  local body
  body=$(printf '{"type":"GATE_REQUIREMENT_REVIEW","stage":"probe","requester":"campaign","task_ref":"GATE-002","context_snapshot":{"artifact_ref":"GATE-002","diff_summary":"d","evidence":["e"],"risk":{"score":5,"level":"L1","factors":["self declared"]},"self_review":""},"deadline":"%s","escalation_policy":{"timeout_action":"auto_approve_l1_only"}}' "$past")
  id=$(curl -s -X POST "$GW/approvals" -H 'Content-Type: application/json' -H 'X-Tenant: default' -d "$body" | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create overdue approval"; return 2; }
  curl -s -o /dev/null -X POST "$GW/admin/sweep"
  status=$(curl -s -H 'X-Tenant: default' "$GW/approvals/$id" | J "['status']")
  echo "overdue approval with caller-declared level=L1: status after sweep=$status"
  if [ "$status" = "approved" ]; then
    echo "FINDING: a self-declared risk level drove a timeout auto-approval with no human"; return 1
  fi
  echo "RESULT: PASS timeout did not auto-approve on a self-declared level"
}

# FSM-001: a task waiting for approval must not resume without a decision.
probe_task_fsm() {
  local id code before after
  id=$(curl -s -X POST "$TC/tasks" -H 'Content-Type: application/json' \
    -d '{"external_ref":"FSM-001","sop":"dev-domain/issue-to-merge"}' | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create task"; return 2; }
  curl -s -o /dev/null -X POST "$TC/tasks/$id/transition" -H 'Content-Type: application/json' -d '{"to":"running"}'
  curl -s -o /dev/null -X POST "$TC/tasks/$id/transition" -H 'Content-Type: application/json' -d '{"to":"waiting_approval"}'
  before=$(curl -s "$TC/tasks/$id" | J "['status']")
  code=$(curl -s -o "$D/tr.json" -w '%{http_code}' -X POST "$TC/tasks/$id/transition" \
    -H 'Content-Type: application/json' -d '{"to":"running","by":"attacker"}')
  after=$(curl -s "$TC/tasks/$id" | J "['status']")
  echo "before=$before transition_http=$code after=$after"
  if [ "$before" != "waiting_approval" ]; then
    echo "INVALID: setup did not reach waiting_approval (got $before)"; return 2
  fi
  if [ "$after" = "running" ] && [ "$code" = "200" ]; then
    echo "FINDING: waiting_approval -> running allowed with no approval decision present"; return 1
  fi
  echo "RESULT: PASS approval wait cannot be bypassed"
}

# FSM-002: an illegal transition must be rejected.
probe_task_fsm_illegal() {
  local id code
  id=$(curl -s -X POST "$TC/tasks" -H 'Content-Type: application/json' \
    -d '{"external_ref":"FSM-002","sop":"dev-domain/issue-to-merge"}' | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create task"; return 2; }
  code=$(curl -s -o "$D/f2.json" -w '%{http_code}' -X POST "$TC/tasks/$id/transition" \
    -H 'Content-Type: application/json' -d '{"to":"done"}')
  echo "pending -> done http=$code"
  [ "$code" != "409" ] && { echo "FINDING: illegal transition not rejected with 409 (got $code)"; return 1; }
  echo "RESULT: PASS illegal transition rejected with 409"
}

# FSM-003: task detail and transition must be tenant-scoped.
probe_task_tenant() {
  local id c_read c_write
  id=$(curl -s -X POST "$TC/tasks" -H 'Content-Type: application/json' \
    -d '{"external_ref":"FSM-003","sop":"dev-domain/issue-to-merge","tenant":"tenantA"}' | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create task"; return 2; }
  c_read=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Tenant: tenantB' "$TC/tasks/$id")
  c_write=$(curl -s -o "$D/t3.json" -w '%{http_code}' -X POST "$TC/tasks/$id/transition" \
    -H 'Content-Type: application/json' -H 'X-Tenant: tenantB' -d '{"to":"running"}')
  echo "task=$id created; tenantB read http=$c_read tenantB transition http=$c_write"
  if [ "$c_read" = "200" ]; then
    echo "FINDING: task-center returned another tenant's task to tenantB (no tenant filter on detail)"; return 1
  fi
  echo "RESULT: PASS task detail is tenant-scoped"
}

# FSM-004: duplicate empty external_ref must not surface as a 500.
probe_task_dup_ref() {
  local code
  curl -s -o /dev/null -X POST "$TC/tasks" -H 'Content-Type: application/json' -d '{"sop":"dev-domain/issue-to-merge"}'
  code=$(curl -s -o "$D/f4.json" -w '%{http_code}' -X POST "$TC/tasks" -H 'Content-Type: application/json' \
    -d '{"sop":"dev-domain/issue-to-merge"}')
  echo "second create with empty external_ref http=$code"
  if [ "$code" = "500" ]; then
    echo "FINDING: duplicate empty external_ref surfaces as HTTP 500 instead of a client error"; return 1
  fi
  echo "RESULT: PASS duplicate external_ref handled without a 500"
}

# RT-001: a rejected run must be archived with a status that reflects rejection.
probe_reject_archive() {
  local tid state archived apv decision_code
  tid=$(curl -s -X POST "$RT/runs" -H 'Content-Type: application/json' \
    -d '{"sop":"product-domain/prd-flow","requirement_text":"probe requirement","task_ref":"RT-001"}' | J "['thread_id']")
  [ -z "$tid" ] && { echo "INVALID: could not start a run"; return 2; }
  # 鉴权专项后 /resume 必须出示真实人审决策（GATE_DECISION_VERIFY=block），
  # 所以这里先在审批台上真做一次 reject，再拿着 approval_id 恢复运行——
  # 这比原来"直接自述 reject"更接近真实链路。
  apv=$(curl -s "$RT/runs/$tid/state" | J "['interrupts'][0]['approval_id']")
  [ -z "$apv" ] && { echo "INVALID: run did not expose an approval_id to decide on"; return 2; }
  decision_code=$(curl -s -o "$D/ra.json" -w '%{http_code}' -X POST "$GW/approvals/$apv/decision" \
    -H 'Content-Type: application/json' -d '{"decision":"reject","decided_by":"probe","comment":"not acceptable"}')
  [ "$decision_code" != "200" ] && { echo "INVALID: could not record the human decision (HTTP $decision_code)"; return 2; }
  state=$(curl -s -X POST "$RT/runs/$tid/resume" -H 'Content-Type: application/json' \
    -d "{\"decision\":\"reject\",\"approval_id\":\"$apv\",\"comment\":\"not acceptable\"}" | J "['status']")
  archived=$(curl -s "$RT/runs/$tid/archive" | J "['status']")
  echo "run=$tid approval=$apv resume_status=$state archive_status=$archived"
  if [ "$state" != "rejected" ]; then
    echo "INVALID: resume did not yield the rejected state (got $state)"; return 2
  fi
  # The archive is the data source for /runs and stats.success_rate, so it must
  # record the rejection itself -- "running" and "waiting" both corrupt the rates.
  if [ "$archived" != "rejected" ]; then
    echo "FINDING: a rejected run is archived as '$archived', so /runs and stats.success_rate cannot see the rejection"; return 1
  fi
  echo "RESULT: PASS archive status records the rejection"
}

# RT-002: waking a run that waits at a gate must not decide it.
probe_wake_guard() {
  local tid before code after
  tid=$(curl -s -X POST "$RT/runs" -H 'Content-Type: application/json' \
    -d '{"sop":"product-domain/prd-flow","requirement_text":"probe requirement","task_ref":"RT-002"}' | J "['thread_id']")
  [ -z "$tid" ] && { echo "INVALID: could not start a run"; return 2; }
  before=$(curl -s "$RT/runs/$tid/state" | J "['next'][0]")
  code=$(curl -s -o "$D/w.json" -w '%{http_code}' -X POST "$RT/runs/$tid/wake" \
    -H 'Content-Type: application/json' -d '{"data":{"note":"probe"}}')
  after=$(curl -s "$RT/runs/$tid/state" | J "['next'][0]")
  echo "before=$before wake_http=$code after=$after"
  if [ "$before" != "gate_g1" ]; then
    echo "INVALID: run is not waiting at the G1 gate (next=$before)"; return 2
  fi
  if [ "$after" != "gate_g1" ]; then
    echo "FINDING: /wake consumed the gate interrupt of a waiting run (next $before -> $after)"; return 1
  fi
  echo "RESULT: PASS wake respected the approval wait"
}

# ---------------------------------------------------------------- P1 proofs

# ALG-001: the heuristic miner must emit correct edge directions.
probe_heuristic() {
  cd "/Users/edy/Desktop/ds-harness/产品研发智能化平台/platform/services/process-mining" || return 2
  ./.venv/bin/python - <<'PY'
import datetime, sys
from app.algorithms import event_log_from_events, heuristic_miner, conformance_heuristic
t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
rows = []
for i in range(5):                      # five traces that only ever do B then A
    for j, act in enumerate(["B", "A"]):
        rows.append((f"c{i}", act, t0 + datetime.timedelta(minutes=i * 10 + j)))
log = event_log_from_events(rows)
net = heuristic_miner(log)
neg = [d for d in net["dependencies"] if d["dependency"] < 0]
conf = conformance_heuristic(log, net)
print("dependencies:", net["dependencies"])
print("self_fitness:", conf["fitness"])
if neg:
    print("FINDING: heuristic miner emitted reversed edges with negative dependency:", neg)
    sys.exit(1)
if conf["fitness"] < 0.999:
    print("FINDING: the mined model rejects the very log it was mined from, fitness", conf["fitness"])
    sys.exit(1)
print("RESULT: PASS heuristic directions are correct and self-conformance is 1.0")
PY
}

# ALG-002: an alpha net must be able to replay the log it was mined from.
probe_alpha() {
  cd "/Users/edy/Desktop/ds-harness/产品研发智能化平台/platform/services/process-mining" || return 2
  ./.venv/bin/python - <<'PY'
import datetime, sys
from app.algorithms import event_log_from_events, alpha_miner, conformance_alpha
t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
cases = {
    "xor3": [["A", "B", "E"], ["A", "C", "E"], ["A", "D", "E"]],
    "par3": [["A", "B", "C", "D", "E"]],
    "loop": [["A", "B", "A", "B", "C"]],
}
bad = []
for name, traces in cases.items():
    rows = []
    for i, acts in enumerate(traces):
        for j, act in enumerate(acts):
            rows.append((f"{name}-{i}", act, t0 + datetime.timedelta(minutes=i * 10 + j)))
    log = event_log_from_events(rows)
    net = alpha_miner(log)
    fit = conformance_alpha(log, net)["fitness"]
    print(f"{name}: places={len(net['places'])} fitness={fit}")
    if fit < 0.999:
        bad.append((name, fit))
if bad:
    print("FINDING: alpha net cannot replay its own log:", bad)
    sys.exit(1)
print("RESULT: PASS alpha self-conformance is consistent")
PY
}

# ALG-003: the default conformance check must detect a unique anomalous path.
probe_dfg_conformance() {
  cd "/Users/edy/Desktop/ds-harness/产品研发智能化平台/platform/services/process-mining" || return 2
  ./.venv/bin/python - <<'PY'
import datetime, sys
from app.algorithms import event_log_from_events, conformance_dfg
t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
traces = [["A", "B", "C"], ["A", "B", "C"], ["A", "B", "C"], ["A", "X", "C"]]
rows = []
for i, acts in enumerate(traces):
    for j, act in enumerate(acts):
        rows.append((f"c{i}", act, t0 + datetime.timedelta(minutes=i * 10 + j)))
log = event_log_from_events(rows)
r = conformance_dfg(log, min_support=1)
print(f"min_support=1: fitness={r['fitness']} deviations={r['deviations_total']}")
if r["deviations_total"] < 1:
    print("FINDING: the unique anomalous path is reported as fully conforming (min_support off-by-one)")
    sys.exit(1)
print("RESULT: PASS dfg conformance detects the unique anomaly")
PY
}

# ALG-004: a malformed LLM response must fall back to rule-based proposals.
# llm_from_env wraps httpx.post(...).json()["choices"]..., so the realistic failure
# mode is a 200 response whose body is not the expected JSON envelope.
probe_llm_fallback() {
  cd "/Users/edy/Desktop/ds-harness/产品研发智能化平台/platform/services/process-mining" || return 2
  ./.venv/bin/python - <<'PYEOF'
import sys
from app import proposals

report = {"bottlenecks": [{"type": "gate_wait", "detail": "G3 waits 30h", "suggestion": "raise trust"}],
          "stats": {"requests": 10}, "gates": [], "cost_by_model": []}

class MalformedResponse:
    status_code = 200
    text = "<html>gateway error</html>"
    def raise_for_status(self):
        return None
    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

class NotChoices:
    status_code = 200
    text = '{"ok":true}'
    def raise_for_status(self):
        return None
    def json(self):
        return {"ok": True}

def make_chat(payload):
    def chat(prompt):
        payload.raise_for_status()
        return payload.json()["choices"][0]["message"]["content"]
    return chat

for label, payload in (("non-JSON body", MalformedResponse()), ("no choices key", NotChoices())):
    try:
        out = proposals.generate(report, chat=make_chat(payload))
    except Exception as exc:
        print("FINDING: " + label + " escaped generate() as " + type(exc).__name__ + ": " + str(exc))
        sys.exit(1)
    if not isinstance(out, list) or not out:
        print("FINDING: " + label + " produced no proposals instead of a rule-based fallback")
        sys.exit(1)
    print(label + ": fell back to " + str(len(out)) + " rule-based proposal(s)")
print("RESULT: PASS malformed LLM responses fall back to rule-based proposals")
PYEOF
}

# EVD-001: evidence upload round-trips and verification confirms the hash.
probe_evidence() {
  local upload verify
  echo "campaign evidence artifact" > "$D/evd.txt"
  upload=$(curl -s -X POST "$GW/evidence" -H 'X-Tenant: default' \
    -F "file=@$D/evd.txt" -F 'task_ref=EVD-001' -F 'acceptance_item=A1' -F 'kind=log')
  echo "upload=$upload" | head -c 300; echo
  verify=$(curl -s -H 'X-Tenant: default' "$GW/evidence/verify?task_ref=EVD-001")
  echo "verify=$verify"
  # 断言与 EVD-001 卡内的真实命令保持一致：按 JSON 字段判断（verified==total 且无 broken）。
  # 原先用 grep 找 "ok": true —— 该字段在 /evidence/verify 的响应契约里并不存在，
  # 属于探针自身错误（会稳定失败），已修正。
  ok=$(printf '%s' "$verify" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print(False); raise SystemExit
print(d.get("verified",0)==d.get("total",-1) and not d.get("broken"))')
  if [ "$ok" != "True" ]; then
    echo "FINDING: evidence verification did not confirm the uploaded artifact"; return 1
  fi
  echo "RESULT: PASS evidence upload and hash verification round-trip"
}

# RT-003: /health must report the checkpointer actually in use.
probe_health_checkpointer() {
  local configured
  configured=$(curl -s "$RT/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["checkpointer"]["configured"])')
  echo "health reports checkpointer.configured=$configured (instance was started with CHECKPOINTER=memory)"
  if [ "$configured" != "memory" ]; then
    echo "FINDING: /health reports a checkpointer different from the configured one"; return 1
  fi
  echo "RESULT: PASS health reports the configured checkpointer"
}

# ---------------------------------------------------------------- dispatch
case "${1:-}" in
  im-callback)        probe_im_callback ;;
  cross-tenant-read)  probe_cross_tenant_read ;;
  resubmit-tenant)    probe_resubmit_tenant ;;
  gate-mode)          probe_gate_mode_override ;;
  gate-persist)       probe_gate_persist ;;
  timeout-auto)       probe_timeout_autoapprove ;;
  task-fsm)           probe_task_fsm ;;
  task-fsm-illegal)   probe_task_fsm_illegal ;;
  task-tenant)        probe_task_tenant ;;
  task-dup-ref)       probe_task_dup_ref ;;
  reject-archive)     probe_reject_archive ;;
  wake-guard)         probe_wake_guard ;;
  heuristic)          probe_heuristic ;;
  alpha)              probe_alpha ;;
  dfg-conformance)    probe_dfg_conformance ;;
  llm-fallback)       probe_llm_fallback ;;
  evidence)           probe_evidence ;;
  health-checkpointer) probe_health_checkpointer ;;
  *) echo "unknown probe: ${1:-<none>}"; exit 2 ;;
esac
