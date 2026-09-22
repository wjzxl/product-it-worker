#!/usr/bin/env bash
# 重建本次战役的 SUT 环境（与 camp-pst-20260922 一致），用于复验修复。
#
# 环境：approval-gateway:18001 / agent-runtime:18002 / task-center:18004
#       一次性 sqlite、ENG_GATES_MODE=block、CHECKPOINTER=memory
# 用法：
#   bash test-campaign/start-sut.sh                    # 前台启动（sqlite）
#   bash test-campaign/start-sut.sh --backend postgres # 用 Postgres（并发专项；库名 pst_sut）
#   bash test-campaign/start-sut.sh --check            # 只做健康检查
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="$ROOT/platform"
D=/tmp/pst-sut
mkdir -p "$D"
LOGS="$D/logs"
mkdir -p "$LOGS"

export ENG_GATES_MODE=block
export CHECKPOINTER=memory
export DEFAULT_TENANT=default
export PROMPTS_DIR="$PLATFORM/prompts"
export PROJECT_RULES_DIR="$PLATFORM/project-rules"
export ROLES_DIR="$PLATFORM/roles"
export TOOLS_DIR="$PLATFORM/tools"
export ENGINEERING_GATES_DIR="$PLATFORM/engineering-gates"
export AMBIGUITY_TUNABLES="$PLATFORM/ambiguity-tunables.yaml"
export SOP_CONFIG_PATH="$PLATFORM/sop-config.yaml"
export APPROVAL_GATEWAY_URL="http://127.0.0.1:18001"
export AGENT_RUNTIME_URL="http://127.0.0.1:18002"
export RUNS_DB_URL="sqlite:///$D/agent_runs-$$.db"
export EVIDENCE_DIR="$D/evidence"

BACKEND="${BACKEND:-sqlite}"
for arg in "$@"; do
  [ "$arg" = "--backend" ] && shift && BACKEND="$1"
done
PG_ADMIN="postgresql://platform:platform@127.0.0.1:5432/postgres"
PG_SUT="postgresql://platform:platform@127.0.0.1:5432/pst_sut"
PG_SUT_SQLA="postgresql+psycopg://platform:platform@127.0.0.1:5432/pst_sut"

if [ "$BACKEND" = "postgres" ]; then
  export SERVICE_AUTH_MODE="${SERVICE_AUTH_MODE:-token}"
  export SERVICE_AUTH_TOKEN="${SERVICE_AUTH_TOKEN:-pst-sut-token}"
  export WEBHOOK_SECRET="${WEBHOOK_SECRET:-pst-sut-webhook}"
  # 鉴权专项：服务令牌 + 入站 webhook 密钥都齐了，再显式要求"恢复人审必须出示真实决策"
  export GATE_DECISION_VERIFY="${GATE_DECISION_VERIFY:-block}"
  export ALERT_WEBHOOK_SECRET="${ALERT_WEBHOOK_SECRET:-${WEBHOOK_SECRET}}"
fi

health() {
  local ok=0
  for p in 18001 18002 18004; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$p/health" || echo 000)
    [ "$code" = "200" ] && echo "  ✅ :$p" || { echo "  ❌ :$p (HTTP $code)"; ok=1; }
  done
  return $ok
}

if [ "${1:-}" = "--check" ]; then health; exit $?; fi

# 一次性数据库：每次启动都是干净环境（与战役一致）
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

start() { # name port dir
  ( cd "$PLATFORM/services/$3" && exec ./.venv/bin/python -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$2" ) >"$LOGS/$1.log" 2>&1 &
  echo $!
}

echo "启动 SUT（sqlite=${D}，ENG_GATES_MODE=${ENG_GATES_MODE}）…"
PIDS=()
PIDS+=("$(start approval-gateway 18001 approval-gateway)")
PIDS+=("$(start agent-runtime 18002 agent-runtime)")

# task-center 用独立 sqlite，避免与 approval-gateway 抢同一个文件
( cd "$PLATFORM/services/task-center" && DATABASE_URL="$TC_DB" \
    AGENT_RUNTIME_URL="$AGENT_RUNTIME_URL" \
    exec ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18004 ) \
  >"$LOGS/task-center.log" 2>&1 &
PIDS+=($!)

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
