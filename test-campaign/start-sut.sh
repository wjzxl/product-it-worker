#!/usr/bin/env bash
# 重建战役 SUT 环境（一次性、可丢弃）。
#
# 环境（camp-pst-20261008 起为 7 服务全量，含跨服务事件投递）：
#   approval-gateway 18001 · agent-runtime 18002 · knowledge-base 18003 · task-center 18004
#   metrics-dashboard 18005 · gitlab-bridge 18006 · process-mining 18007
#
# 隔离约定（测试数据不得落到非一次性环境）：
#   · 数据落一次性库 pst_sut（每次启动重建）；sqlite 模式落 /tmp/pst-sut
#   · SOP 配置写入 **临时副本**，不改仓库里的 sop-config.yaml
#   · 知识库写入战役专用 collection（KB_COLLECTION），不污染演示语料
#   · 证据落 /tmp/pst-sut/evidence
#
# 用法：
#   bash test-campaign/start-sut.sh                    # 前台启动（sqlite）
#   bash test-campaign/start-sut.sh --backend postgres # Postgres（并发/迁移/备份专项）
#   bash test-campaign/start-sut.sh --check            # 只做健康检查
#   bash test-campaign/start-sut.sh --services 3       # 只起前 3 个服务（老探针够用）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="$ROOT/platform"
D=/tmp/pst-sut
mkdir -p "$D"
LOGS="$D/logs"
mkdir -p "$LOGS"

# SOP 配置用临时副本：process-mining 的「应用提案」会写这个文件
cp -f "$PLATFORM/sop-config.yaml" "$D/sop-config.yaml"

export ENG_GATES_MODE=block
export CHECKPOINTER=memory
export DEFAULT_TENANT=default
export PROMPTS_DIR="$PLATFORM/prompts"
export PROJECT_RULES_DIR="$PLATFORM/project-rules"
export ROLES_DIR="$PLATFORM/roles"
export TOOLS_DIR="$PLATFORM/tools"
export ENGINEERING_GATES_DIR="$PLATFORM/engineering-gates"
export AMBIGUITY_TUNABLES="$PLATFORM/ambiguity-tunables.yaml"
export SOP_CONFIG_PATH="$D/sop-config.yaml"
export RUNS_DB_URL="sqlite:///$D/agent_runs-$$.db"
export EVIDENCE_DIR="$D/evidence"

GW=http://127.0.0.1:18001
RT=http://127.0.0.1:18002
KB=http://127.0.0.1:18003
TC=http://127.0.0.1:18004
MD=http://127.0.0.1:18005
GL=http://127.0.0.1:18006
PM=http://127.0.0.1:18007
export APPROVAL_GATEWAY_URL="$GW"
export AGENT_RUNTIME_URL="$RT"
export KNOWLEDGE_BASE_URL="$KB"
export TASK_CENTER_URL="$TC"     # 知识库校验项目授权要用（A1 契约）
export GITLAB_BRIDGE_URL="$GL"
# 事件真的投递到 task-center（此前 SUT 不设，事件只落日志——跨服务联动从未被跑过）
export PUBLISH_TARGETS="$TC/events"

BACKEND="${BACKEND:-sqlite}"
SERVICES=7
for arg in "$@"; do
  case "$arg" in
    --backend) shift; BACKEND="$1" ;;
    --services) shift; SERVICES="$1" ;;
  esac
done
PG_SUT_SQLA="postgresql+psycopg://platform:platform@127.0.0.1:5432/pst_sut"

if [ "$BACKEND" = "postgres" ]; then
  export SERVICE_AUTH_MODE="${SERVICE_AUTH_MODE:-token}"
  export SERVICE_AUTH_TOKEN="${SERVICE_AUTH_TOKEN:-pst-sut-token}"
  export WEBHOOK_SECRET="${WEBHOOK_SECRET:-pst-sut-webhook}"
  export GATE_DECISION_VERIFY="${GATE_DECISION_VERIFY:-block}"
  export ALERT_WEBHOOK_SECRET="${ALERT_WEBHOOK_SECRET:-${WEBHOOK_SECRET}}"
  # 审批台的人工入口：个人静态令牌（控制台用它换 Bearer）
  export AUTH_MODE="${AUTH_MODE:-static}"
  export AUTH_STATIC_TOKEN="${AUTH_STATIC_TOKEN:-pst-sut-personal-token}"
  # 内部代理密钥：TRUST_PROXY_AUTH=1 时必须独立配置（伪造 X-Platform-User 因此无效）
  export PROXY_AUTH_TOKEN="${PROXY_AUTH_TOKEN:-pst-sut-proxy-token}"
  # 主体授权范围（新契约：主体必须被显式授予以租户与角色，否则 403）
  # 战役用共享服务身份跑探针；生产应改用 SERVICE_AUTH_CREDENTIALS 做逐服务最小授权。
  export SERVICE_TENANTS="${SERVICE_TENANTS:-default,campaign,authz,conc,stress,tenantA,tenantB,victim,acme,globex,line-a,drill}"
  export SERVICE_ROLES="${SERVICE_ROLES:-admin}"
  # 归档与度量同库（否则驾驶舱读到 0 次运行）
  export RUNS_DB_URL="$PG_SUT_SQLA"
fi

PORTS=(18001 18002 18003 18004 18005 18006 18007)
health() {
  local ok=0 i=0
  for p in "${PORTS[@]}"; do
    i=$((i + 1))
    [ "$i" -le "$SERVICES" ] || break
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:${p}/health" || echo 000)
    [ "$code" = "200" ] && echo "  ✅ :${p}" || { echo "  ❌ :${p} (HTTP ${code})"; ok=1; }
  done
  return $ok
}

if [ "${1:-}" = "--check" ]; then health; exit $?; fi

if [ "$BACKEND" = "postgres" ]; then
  docker exec platform-pg psql -U platform -d postgres -c "DROP DATABASE IF EXISTS pst_sut" >/dev/null 2>&1
  docker exec platform-pg psql -U platform -d postgres -c "CREATE DATABASE pst_sut" >/dev/null 2>&1
  export DATABASE_URL="$PG_SUT_SQLA"
  TC_DB="$PG_SUT_SQLA"
  echo "使用 Postgres 后端：pst_sut（每次启动重建，含 SERVICE_AUTH_MODE=token）"
else
  rm -f "$D/approval.db" "$D/task.db"
  export DATABASE_URL="sqlite:///$D/approval.db"
  TC_DB="sqlite:///$D/task.db"
fi

start() { # name port dir [extra env...]
  local name="$1" port="$2" dir="$3"; shift 3
  ( cd "$PLATFORM/services/$dir" && env "$@" ./.venv/bin/python -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$port" ) >"$LOGS/$name.log" 2>&1 &
  echo $!
}

PIDS=()
echo "启动 SUT（backend=${BACKEND}，services=${SERVICES}）…"
PIDS+=("$(start approval-gateway 18001 approval-gateway)")
PIDS+=("$(start agent-runtime 18002 agent-runtime)")
if [ "$SERVICES" -ge 3 ]; then
  PIDS+=("$(start knowledge-base 18003 knowledge-base \
      QDRANT_URL=http://127.0.0.1:6333 KB_COLLECTION=pst_sut_kb SANDBOX_MODE=host)")
fi
( cd "$PLATFORM/services/task-center" && DATABASE_URL="$TC_DB" AGENT_RUNTIME_URL="$AGENT_RUNTIME_URL" \
    exec ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18004 ) \
  >"$LOGS/task-center.log" 2>&1 &
PIDS+=($!)
if [ "$SERVICES" -ge 5 ]; then
  # 驾驶舱按**文档中的生产拓扑**跑：入口代理（Caddy/SSO）注入 X-Platform-User。
  # 战役用 ui/proxy.py 模拟入口（含剥离客户端自带的同名头），这样 TRUST_PROXY_AUTH
  # 这条此前从未被跑过的路径能真的被验证。
  # 度量必须读**本战役的库**（pst_sut），否则驾驶舱显示的是别的库的历史数据，
  # UI 卡就测不到"系统当前状态"；LiteLLM 计量库保持指向 litellm。
  PIDS+=("$(start metrics-dashboard 18005 metrics-dashboard \
      TRUST_PROXY_AUTH=1 PROXY_AUTH_TOKEN=$PROXY_AUTH_TOKEN PROXY_ROLES=platform_admin \
      PLATFORM_DSN='host=127.0.0.1 port=5432 dbname=pst_sut user=platform password=platform' \
      LITELLM_DSN='host=127.0.0.1 port=5432 dbname=litellm user=platform password=platform')")
  PIDS+=("$(start gitlab-bridge 18006 gitlab-bridge \
      GITLAB_TOKEN= WEBHOOK_SECRET="${WEBHOOK_SECRET:-pst-sut-webhook}")")
  PIDS+=("$(start process-mining 18007 process-mining \
      SOP_CONFIG_PATH=$D/sop-config.yaml APPROVAL_GATEWAY_URL=$GW GITLAB_BRIDGE_URL=$GL \
      KNOWLEDGE_BASE_URL=$KB)") 
fi

trap 'echo; echo "停止 SUT…"; for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null' INT TERM

echo "等待健康检查…"
for _ in $(seq 1 40); do
  if health >/dev/null 2>&1; then break; fi
  sleep 1.5
done
health

echo
echo "环境就绪。日志：$LOGS/"
echo "复验示例：bash $ROOT/test-campaign/probes.sh im-callback"
wait
