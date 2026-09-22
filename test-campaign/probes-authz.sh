#!/usr/bin/env bash
# 专项探针 2：鉴权/越权（原报告第 11 节列出的未覆盖维度）
#
# 运行前提：SUT 用 Postgres 后端（会带 SERVICE_AUTH_MODE=token）
#   bash test-campaign/start-sut.sh --backend postgres
#
# 每个子命令打印 "RESULT: PASS ..." 或 "FINDING: ..."；exit 1 = 产品缺陷，exit 2 = 探针无效。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM="$(cd "$DIR/.." && pwd)/platform"
GW=${GW:-http://127.0.0.1:18001}
RT=${RT:-http://127.0.0.1:18002}
TC=${TC:-http://127.0.0.1:18004}
TOKEN=${SERVICE_AUTH_TOKEN:-pst-sut-token}
ALERT_SECRET=${ALERT_WEBHOOK_SECRET:-${WEBHOOK_SECRET:-pst-sut-webhook}}
D=/tmp/pst-sut
mkdir -p "$D"
J() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }

code() { curl -s -o /dev/null -w '%{http_code}' -m 15 "$@"; }

# ---------------------------------------------------------------------------
# AUTHZ-001：端点鉴权清单 —— 33 个写接口里还有几个是"裸"的
probe_open_writes() {
  local open_now open_without_mw
  python3 "$PLATFORM/scripts/audit_endpoint_auth.py" | grep -E "合计|豁免" || true
  open_now=$(python3 "$PLATFORM/scripts/audit_endpoint_auth.py" | grep "^合计" | grep -o '无鉴权 [0-9]*' | grep -o '[0-9]*')
  open_without_mw=$(python3 "$PLATFORM/scripts/audit_endpoint_auth.py" --ignore-middleware | grep "^合计" | grep -o '无鉴权 [0-9]*' | grep -o '[0-9]*')
  echo "逐路由依赖口径下无鉴权写接口=${open_without_mw}（中间件接管前的历史值），含中间件口径=$open_now"
  if [ "${open_now:-1}" -ne 0 ]; then
    echo "FINDING: 仍有 $open_now 个写接口没有任何鉴权覆盖"; return 1
  fi
  if [ "${open_without_mw:-0}" -eq 0 ]; then
    echo "INVALID: 逐路由口径也为 0，说明该服务并非靠中间件覆盖（本探针失去对照意义）"; return 2
  fi
  echo "RESULT: PASS 写接口全部有鉴权覆盖（$open_without_mw 个由统一中间件接管）"
}

# ---------------------------------------------------------------------------
# AUTHZ-002：无令牌的写请求必须被拒（三服务代表端点）
probe_no_token() {
  local fails=0 out
  check_401() { # $1=name $2=expected($3=code)
    local name="$1" got="$2" want="$3"
    if [ "$got" = "$want" ]; then out="  $name → ${got}（期望 ${want}）";
    else out="  $name → ${got}（期望 ${want}）✗"; fails=$((fails+1)); fi
    echo "$out"
  }
  check_401 "POST /approvals                " \
    "$(code -X POST "$GW/approvals" -H 'Content-Type: application/json' -d '{"type":"GATE_REQUIREMENT_REVIEW"}')" 401
  check_401 "POST /tasks                    " \
    "$(code -X POST "$TC/tasks" -H 'Content-Type: application/json' -d '{"sop":"dev-domain/issue-to-merge"}')" 401
  check_401 "POST /runs                     " \
    "$(code -X POST "$RT/runs" -H 'Content-Type: application/json' -d '{"requirement_text":"x"}')" 401
  check_401 "POST /tools/invoke             " \
    "$(code -X POST "$RT/tools/invoke" -H 'Content-Type: application/json' -d '{"tool":"kb.search","params":{}}')" 401
  check_401 "POST /admin/sweep              " "$(code -X POST "$GW/admin/sweep")" 401
  check_401 "GET  /approvals（读也受保护）  " "$(code "$GW/approvals")" 401
  check_401 "GET  /health（默认豁免）       " "$(code "$GW/health")" 200
  if [ "$fails" -ne 0 ]; then echo "FINDING: $fails 个端点鉴权行为不符合预期"; return 1; fi
  echo "RESULT: PASS 无令牌的读写请求被拒，健康检查豁免生效"
}

# ---------------------------------------------------------------------------
# AUTHZ-003：带令牌的正常链路必须仍然可用（鉴权不能把功能一起关掉）
probe_with_token() {
  local id
  id=$(curl -s -m 15 -X POST "$GW/approvals" -H "X-Service-Token: $TOKEN" \
        -H 'Content-Type: application/json' -H 'X-Tenant: authz' \
        -d '{"type":"GATE_REQUIREMENT_REVIEW","stage":"authz","requester":"probe","task_ref":"AUTHZ-003","context_snapshot":{"artifact_ref":"a","diff_summary":"d","evidence":["e"],"risk":{"score":20,"level":"L2","factors":["probe"]}},"deadline":"2030-01-01T00:00:00+00:00","escalation_policy":{"remind_after_hours":1,"escalate_to":"lead","timeout_action":"reject"}}' | J "['id']")
  [ -z "$id" ] && { echo "INVALID: 带令牌创建审批失败（服务是否在 token 模式？）"; return 2; }
  local got
  # 注意：必须带同一个租户头——单据是按租户隔离的，不带就是 404（隔离本身是对的）
  got=$(code -X POST "$GW/approvals/$id/decision" -H "X-Service-Token: $TOKEN" -H 'X-Tenant: authz' \
        -H 'Content-Type: application/json' -d '{"decision":"approve","decided_by":"probe","comment":"带令牌"}')
  if [ "$got" != "200" ]; then echo "FINDING: 带正确令牌的决策被拒（${got}）——鉴权把功能也关掉了"; return 1; fi
  echo "RESULT: PASS 带服务令牌的审批与决策链路正常（approval=${id}）"
}

# ---------------------------------------------------------------------------
# AUTHZ-004：人审闸门不得被自述决策绕过（/resume 必须出示真实审批）
probe_resume_bypass() {
  local tid body got
  tid=$(curl -s -m 15 -X POST "$RT/runs" -H "X-Service-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d '{"requirement_text":"鉴权专项：闸门绕过探针","task_ref":"AUTHZ-004"}' | J "['thread_id']")
  [ -z "$tid" ] && { echo "INVALID: 无法创建运行"; return 2; }
  body=$(curl -s -m 10 "$RT/runs/$tid/state" -H "X-Service-Token: $TOKEN")
  echo "$body" | grep -q 'gate_g1' || { echo "INVALID: 运行未挂在 G1（${body}）"; return 2; }

  got=$(code -X POST "$RT/runs/$tid/resume" -H "X-Service-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d '{"decision":"approve","comment":"我是调用方，我说批准了"}')
  if [ "$got" != "409" ]; then
    echo "FINDING: 不带 approval_id 的自述决策竟被接受（HTTP ${got}）——人审闸门可绕过"; return 1
  fi
  got=$(code -X POST "$RT/runs/$tid/resume" -H "X-Service-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d '{"decision":"approve","approval_id":"apv_not_exist","comment":"伪造编号"}')
  if [ "$got" != "409" ]; then
    echo "FINDING: 伪造的 approval_id 竟能恢复执行（HTTP ${got}）"; return 1
  fi
  # 闸门必须还在原地
  if ! curl -s -m 10 "$RT/runs/$tid/state" -H "X-Service-Token: $TOKEN" | grep -q 'gate_g1'; then
    echo "FINDING: 被拒的 resume 改变了流程状态（拒绝了却没拒绝干净）"; return 1
  fi
  echo "RESULT: PASS 无据/伪造决策均被拒（409），闸门保持在原地"
}

# ---------------------------------------------------------------------------
# AUTHZ-005：工具调用不得自述岗位、不得自述免人审
probe_tools_selfdeclared() {
  local body got reason
  body=$(curl -s -m 15 -X POST "$RT/tools/invoke" -H "X-Service-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d '{"tool":"ops.execute","params":{"action":"restart","target":"x"},"role":"ops-duty","allow_ungated":true}')
  reason=$(echo "$body" | J "['detail']['reason']")
  if [ "$reason" != "ungated_not_allowed" ]; then
    echo "FINDING: 请求体自带的 allow_ungated 被采信（reason=${reason}）——人审依据可由调用方自行豁免"; return 1
  fi
  body=$(curl -s -m 15 -X POST "$RT/tools/invoke" -H "X-Service-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d '{"tool":"kb.search","params":{"query":"x"},"role":"dev-agent"}')
  reason=$(echo "$body" | J "['detail']['reason']")
  if [ "$reason" != "role_not_trusted" ]; then
    echo "FINDING: 受管控部署下请求体自述的 role 被采信（reason=${reason}）——可自称任意岗位越权"; return 1
  fi
  echo "RESULT: PASS 自述岗位/自述免人审均被拒（ungated_not_allowed / role_not_trusted）"
}

# ---------------------------------------------------------------------------
# AUTHZ-006：告警 webhook 必须验签（此前完全裸奔）
probe_alert_signature() {
  local payload got sig
  payload='{"status":"firing","alerts":[{"status":"firing","labels":{"alertname":"AuthzProbe","fingerprint":"authz1"}}]}'
  got=$(code -X POST "$TC/alerts/prometheus" -H 'Content-Type: application/json' -d "$payload")
  [ "$got" = "401" ] || { echo "FINDING: 无凭据的告警 webhook 未被拒（HTTP ${got}）——可凭空拉起运维 SOP"; return 1; }

  got=$(code -X POST "$TC/alerts/prometheus" -H 'Content-Type: application/json' \
        -H 'X-Webhook-Token: wrong-secret' -d "$payload")
  [ "$got" = "401" ] || { echo "FINDING: 错误密钥未被拒（HTTP ${got}）"; return 1; }

  got=$(code -X POST "$TC/alerts/prometheus" -H 'Content-Type: application/json' \
        -H "X-Webhook-Token: $ALERT_SECRET" -d "$payload")
  [ "$got" = "200" ] || { echo "FINDING: 正确密钥的告警被拒（HTTP ${got}）——把监控接进来的路也堵死了"; return 1; }

  sig=$(python3 -c "
import hashlib,hmac
print('sha256='+hmac.new('$ALERT_SECRET'.encode(), '''$payload'''.encode(), hashlib.sha256).hexdigest())")
  got=$(code -X POST "$TC/alerts/prometheus" -H 'Content-Type: application/json' \
        -H "X-Webhook-Signature: $sig" -d "$payload")
  [ "$got" = "200" ] || { echo "FINDING: 正确 HMAC 签名的告警被拒（HTTP ${got}）"; return 1; }
  echo "RESULT: PASS 告警 webhook 无凭据/错密钥被拒，令牌与 HMAC 两种正确凭据均放行"
}

# ---------------------------------------------------------------------------
# AUTHZ-007：并发决策不得绕过唯一键（与并发专项交叉验证）
probe_decide_authz_race() {
  python3 "$DIR/race_stress.py" --target approval --concurrency "${STRESS_N:-20}" \
    --rounds "${STRESS_ROUNDS:-3}" --gateway "$GW" --token "$TOKEN"
}

case "${1:-}" in
  open-writes)        probe_open_writes ;;
  no-token)           probe_no_token ;;
  with-token)         probe_with_token ;;
  resume-bypass)      probe_resume_bypass ;;
  tools-selfdeclared) probe_tools_selfdeclared ;;
  alert-signature)    probe_alert_signature ;;
  decide-race)        probe_decide_authz_race ;;
  *) echo "unknown probe: ${1:-<none>}"; exit 2 ;;
esac
