#!/usr/bin/env bash
# 专项探针 2：并发/竞态（原报告第 11 节列出的未覆盖维度）
#
# 运行前提：SUT 使用 Postgres 后端（sqlite 会把写操作串行化，测不出真实竞态）
#   bash test-campaign/start-sut.sh --backend postgres
#
# 每个子命令打印一行 "RESULT: PASS ..." 或 "FINDING: ..."，exit 1 表示产品缺陷，
# exit 2 表示探针自身无效（环境不满足）。
set -uo pipefail

GW=${GW:-http://127.0.0.1:18001}
TC=${TC:-http://127.0.0.1:18004}
RT=${RT:-http://127.0.0.1:18002}
TOKEN=${SERVICE_AUTH_TOKEN:-pst-sut-token}
D=/tmp/pst-sut
mkdir -p "$D"
J() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }

# 带服务令牌的请求头（SERVICE_AUTH_MODE=token 时必需；未开启时多带也无害）
AUTH=(-H "X-Service-Token: $TOKEN")

psql_sut() { docker exec platform-pg psql -U platform -d pst_sut -tAc "$1" 2>/dev/null; }

approval_body() { # $1=ref $2=level $3=deadline_iso
  printf '{"type":"GATE_REQUIREMENT_REVIEW","stage":"probe","requester":"campaign","task_ref":"%s","context_snapshot":{"artifact_ref":"%s","diff_summary":"probe","evidence":["http://evidence/1"],"risk":{"score":20,"level":"%s","factors":["probe"]}},"deadline":"%s","escalation_policy":{"remind_after_hours":1,"escalate_to":"lead","timeout_action":"reject"}}' \
    "$1" "$1" "$2" "$3"
}

past() { python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=5)).isoformat())'; }
future() { python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=5)).isoformat())'; }

mk_approval() { # $1=ref $2=deadline
  curl -s -X POST "$GW/approvals" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d "$(approval_body "$1" L2 "$2")" | J "['id']"
}

# ---------------------------------------------------------------------------
# CONC-001：同一审批单被并发决策 —— 只能有一次成功，且只能落一条决策
probe_decide_race() {
  local id before after ok cnt
  id=$(mk_approval "CONC-001" "$(future)")
  [ -z "$id" ] && { echo "INVALID: could not create approval"; return 2; }
  before=$(psql_sut "select count(*) from approval_decisions where request_id='$id'")

  for i in $(seq 1 8); do
    curl -s -o "$D/race-$i.json" -w '%{http_code}' -X POST "$GW/approvals/$id/decision" "${AUTH[@]}" \
      -H 'Content-Type: application/json' \
      -d '{"decision":"approve","decided_by":"racer-'$i'","comment":"concurrent"}' > "$D/race-$i.code" &
  done
  wait
  ok=$(grep -l '^200$' "$D"/race-*.code 2>/dev/null | wc -l | tr -d ' ')
  after=$(psql_sut "select count(*) from approval_decisions where request_id='$id'")
  cnt=$((after - before))
  echo "concurrent decisions: success_http_200=$ok decisions_inserted=$cnt"
  if [ "$cnt" -ne 1 ]; then
    echo "FINDING: concurrent decisions inserted $cnt decision rows (expected exactly 1)"; return 1
  fi
  if [ "$ok" -ne 1 ]; then
    echo "FINDING: $ok requests reported success for one approval (expected exactly 1)"; return 1
  fi
  echo "RESULT: PASS concurrent decisions are serialised to a single decision"
}

# ---------------------------------------------------------------------------
# CONC-002：人工决策与超时扫描同时发生 —— 不得产生两条决策
probe_decide_vs_sweep() {
  local id before after cnt status
  id=$(mk_approval "CONC-002" "$(past)")
  [ -z "$id" ] && { echo "INVALID: could not create approval"; return 2; }
  before=$(psql_sut "select count(*) from approval_decisions where request_id='$id'")

  curl -s -o /dev/null -X POST "$GW/approvals/$id/decision" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d '{"decision":"approve","decided_by":"human","comment":"race with sweep"}' &
  curl -s -o /dev/null -X POST "$GW/admin/sweep" "${AUTH[@]}" &
  wait

  after=$(psql_sut "select count(*) from approval_decisions where request_id='$id'")
  cnt=$((after - before))
  status=$(psql_sut "select status from approval_requests where id='$id'")
  echo "decisions_inserted=$cnt final_status=$status"
  if [ "$cnt" -ne 1 ]; then
    echo "FINDING: decide+sweep produced $cnt decisions for one approval"; return 1
  fi
  echo "RESULT: PASS decide and sweep do not double-decide"
}

# ---------------------------------------------------------------------------
# CONC-003：签名 IM 回调与 Web 决策并发 —— 只能有一条决策
probe_im_vs_decide() {
  local id before after cnt
  id=$(mk_approval "CONC-003" "$(future)")
  [ -z "$id" ] && { echo "INVALID: could not create approval"; return 2; }
  before=$(psql_sut "select count(*) from approval_decisions where request_id='$id'")

  # IM 回调需要签名密钥；SUT 用 WEBHOOK_SECRET 作为 IM 签名密钥的兜底
  local body ts nonce sig
  body=$(printf '{"approval_id":"%s","action":"approve","operator":"im","comment":"race"}' "$id")
  ts=$(python3 -c 'import time;print(int(time.time()))')
  nonce="conc3"
  sig=$(python3 -c "
import hashlib,sys
key='${IM_CALLBACK_SIGNING_KEY:-${WEBHOOK_SECRET:-pst-sut-webhook}}'
print(hashlib.sha256(('$ts'+'$nonce'+key+'$body').encode()).hexdigest())")

  curl -s -o /dev/null -X POST "$GW/callbacks/im/card" \
    -H "X-Lark-Request-Timestamp: $ts" -H "X-Lark-Request-Nonce: $nonce" \
    -H "X-Lark-Signature: $sig" -H 'Content-Type: application/json' -d "$body" &
  curl -s -o /dev/null -X POST "$GW/approvals/$id/decision" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d '{"decision":"approve","decided_by":"web","comment":"race"}' &
  wait

  after=$(psql_sut "select count(*) from approval_decisions where request_id='$id'")
  cnt=$((after - before))
  echo "decisions_inserted=$cnt"
  if [ "$cnt" -ne 1 ]; then
    echo "FINDING: IM callback and web decision both landed ($cnt decisions)"; return 1
  fi
  echo "RESULT: PASS IM callback and web decision are mutually exclusive"
}

# ---------------------------------------------------------------------------
# CONC-004：同一验收项并发上传证据 —— 全部成功且不丢件
probe_evidence_race() {
  local before after cnt fails=0
  echo "race artifact" > "$D/evd-race.txt"
  before=$(psql_sut "select count(*) from evidence_artifacts where task_ref='CONC-004'")

  for i in $(seq 1 6); do
    ( code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/evidence" "${AUTH[@]}" \
        -H 'X-Tenant: default' -F "file=@$D/evd-race.txt" -F 'task_ref=CONC-004' \
        -F 'acceptance_item=A1' -F 'kind=log' -F "note=race-$i"); echo "$code" > "$D/evd-race-$i.code" ) &
  done
  wait
  for i in $(seq 1 6); do
    [ "$(cat "$D/evd-race-$i.code" 2>/dev/null)" = "201" ] || fails=$((fails+1))
  done
  after=$(psql_sut "select count(*) from evidence_artifacts where task_ref='CONC-004'")
  cnt=$((after - before))
  echo "uploads=6 failed=$fails rows_inserted=$cnt"
  if [ "$fails" -ne 0 ] || [ "$cnt" -ne 6 ]; then
    echo "FINDING: concurrent evidence upload lost artifacts (failed=$fails inserted=$cnt)"; return 1
  fi
  echo "RESULT: PASS concurrent evidence uploads all persisted"
}

# ---------------------------------------------------------------------------
# CONC-005：并发任务状态流转 —— 只能有一次成功
probe_task_transition_race() {
  local id ok
  id=$(curl -s -X POST "$TC/tasks" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -H 'X-Tenant: conc' -d '{"sop":"dev-domain/issue-to-merge"}' | J "['id']")
  [ -z "$id" ] && { echo "INVALID: could not create task"; return 2; }
  curl -s -o /dev/null -X POST "$TC/tasks/$id/transition" "${AUTH[@]}" -H 'X-Tenant: conc' \
    -H 'Content-Type: application/json' -d '{"to":"running","by":"agent"}'
  curl -s -o /dev/null -X POST "$TC/tasks/$id/transition" "${AUTH[@]}" -H 'X-Tenant: conc' \
    -H 'Content-Type: application/json' -d '{"to":"waiting_approval","by":"agent"}'

  for i in 1 2 3 4; do
    curl -s -o /dev/null -w '%{http_code}' -X POST "$TC/tasks/$id/transition" "${AUTH[@]}" -H 'X-Tenant: conc' \
      -H 'Content-Type: application/json' \
      -d '{"to":"running","by":"human'$i'","detail":{"approval_id":"apv_x","decided_by":"racer"}}' > "$D/tt-$i.code" &
  done
  wait
  ok=$(grep -l '^200$' "$D"/tt-*.code 2>/dev/null | wc -l | tr -d ' ')
  echo "concurrent transitions succeeded=$ok"
  if [ "$ok" -ne 1 ]; then
    echo "FINDING: $ok concurrent transitions succeeded (expected exactly 1)"; return 1
  fi
  echo "RESULT: PASS concurrent task transitions are serialised"
}

# ---------------------------------------------------------------------------
# CONC-006：把并发拉满（默认 30 线程 × 8 轮）——2–8 并发的"没撞上"不算证据
probe_stress() { # $1=approval|task
  python3 "$(dirname "$0")/race_stress.py" --target "$1" \
    --concurrency "${STRESS_N:-30}" --rounds "${STRESS_ROUNDS:-8}" \
    --gateway "$GW" --task-center "$TC" --token "$TOKEN"
}

case "${1:-}" in
  decide-race)        probe_decide_race ;;
  decide-vs-sweep)    probe_decide_vs_sweep ;;
  im-vs-decide)       probe_im_vs_decide ;;
  evidence-race)      probe_evidence_race ;;
  task-transition-race) probe_task_transition_race ;;
  decide-race-stress)   probe_stress approval ;;
  task-race-stress)     probe_stress task ;;
  *) echo "unknown probe: ${1:-<none>}"; exit 2 ;;
esac
