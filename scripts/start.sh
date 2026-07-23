#!/bin/bash
# v31 APP_MODE startup — single session per account scope.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/detach_exec.sh
source "${ROOT}/scripts/lib/detach_exec.sh"
export PATH="${ROOT}/.venv/bin:${PATH}"
export PYTHONPATH="${ROOT}/src"

APP_MODE="${APP_MODE:-}"
CONFIG=""
PORT=""
PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3 || true)"
fi

usage() {
  echo "Usage: $0 --mode DEMO|LIVE|TESTBED [--config PATH] [--port PORT]" >&2
  echo "  APP_MODE env is used when --mode is omitted." >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      APP_MODE="${2:-}"
      shift 2
      ;;
    --config)
      CONFIG="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [[ -z "${APP_MODE}" ]]; then
  echo "ERROR: APP_MODE is required — use --mode DEMO|LIVE|TESTBED or export APP_MODE" >&2
  exit 1
fi

APP_MODE="$(printf '%s' "${APP_MODE}" | tr '[:lower:]' '[:upper:]')"
case "${APP_MODE}" in
  DEMO|LIVE|TESTBED) ;;
  *)
    echo "ERROR: invalid APP_MODE=${APP_MODE} — expected DEMO, LIVE, or TESTBED" >&2
    exit 1
    ;;
esac

# Default config overlay per mode
if [[ -z "${CONFIG}" ]]; then
  case "${APP_MODE}" in
    DEMO)
      if [[ -f "${ROOT}/config/config_v31.json" ]]; then
        CONFIG="config/config_v31.json"
      else
        CONFIG="config/config_v29.json"
      fi
      ;;
    LIVE)
      CONFIG="config/config_v31_live_canary.json"
      ;;
    TESTBED)
      CONFIG="config/config_v31_testbed.json"
      ;;
  esac
fi

# Default port per mode
if [[ -z "${PORT}" ]]; then
  if [[ "${APP_MODE}" == "TESTBED" ]]; then
    PORT="9199"
  else
    PORT="8080"
  fi
fi

# LIVE fail-closed arm gate
if [[ "${APP_MODE}" == "LIVE" ]]; then
  allow="${IG_ALLOW_LIVE:-}"
  case "${allow}" in
    1|true|yes|on|TRUE|YES|ON) export IG_ALLOW_LIVE=1 ;;
    *)
      echo "ERROR: APP_MODE=LIVE rejected — set IG_ALLOW_LIVE=1 to arm live trading" >&2
      exit 2
      ;;
  esac
fi

export APP_MODE
export IG_AGENT_CONFIG="${CONFIG}"
export IG_API_PORT="${PORT}"

# Resolve data root + account scope via Python (sets IG_DATA_ROOT / IG_ACCOUNT_SCOPE)
DATA_ROOT="$("${PY}" - <<'PY'
import os
from runtime.app_mode import AppMode, parse_app_mode, resolve_data_root
from runtime.session_lock import resolve_account_scope

mode = parse_app_mode(os.environ["APP_MODE"])
root = resolve_data_root(mode)
os.environ["IG_DATA_ROOT"] = root
try:
    scope = resolve_account_scope(mode)
    os.environ["IG_ACCOUNT_SCOPE"] = scope
except Exception as exc:
    if mode.value != "TESTBED":
        raise SystemExit(f"account scope resolve failed: {exc}") from exc
    os.environ["IG_ACCOUNT_SCOPE"] = "testbed:local"
print(root)
PY
)"
export IG_DATA_ROOT="${DATA_ROOT}"

# Broker plane bundle
case "${APP_MODE}" in
  DEMO) export IG_BROKER_PLANE=DEMO ;;
  LIVE) export IG_BROKER_PLANE=LIVE ;;
  TESTBED) export IG_BROKER_PLANE=MOCK ;;
esac

mkdir -p "${IG_DATA_ROOT}/logs"

echo "🔒 Session preflight (mode=${APP_MODE} port=${PORT} scope=${IG_ACCOUNT_SCOPE:-unknown})..."
if ! "${PY}" -m runtime.session_lock preflight --mode "${APP_MODE}" --port "${PORT}" --config "${CONFIG}"; then
  rc=$?
  echo "❌ Session preflight failed (exit ${rc})" >&2
  exit "${rc}"
fi

echo "🧪 RUNNING COMPILATION AND TESTING SUITE..."
TEST_FILES=(
  tests/test_dual_core_execution.py
  tests/test_v31_telemetry.py
  tests/test_broker_epic_resolver.py
  tests/test_broker_reject_guard.py
  tests/test_live_canary_guards.py
  tests/test_live_canary_session.py
  tests/test_app_mode_session_lock.py
  tests/test_shutdown_and_health_identity.py
  tests/test_gui_session_attach.py
  tests/test_trade_pipeline_health.py
  tests/test_pipeline_governance.py
  tests/test_strategy_profile.py
  tests/test_strategy_selector.py
  tests/test_strategy_controller.py
  tests/test_strategy_transition.py
  tests/test_strategy_enforcement.py
  tests/test_hard_enforcement.py
  tests/test_adaptive_thresholds.py
  tests/test_strategy_performance_memory.py
  tests/test_regime_detection.py
  tests/test_regime_aware_selector.py
  tests/test_regime_risk_envelope.py
  tests/test_regime_sizing.py
  tests/test_daily_pnl_targeting.py
  tests/test_unified_execution.py
  tests/test_unified_routing_boot_warmup.py
  tests/test_strategy_governance.py
  tests/test_session_review.py
  tests/test_full_system_stress.py
)
if pytest "${TEST_FILES[@]}" -p no:anyio; then
  echo "✅ TESTS PASSED GREEN. RE-ARMING RUNTIME..."
else
  echo "❌ TESTS FAILED. CORE BLOCKED FROM LIVE DEPLOYMENT."
  exit 1
fi

SUP_LOG="${IG_DATA_ROOT}/logs/supervisor.log"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

if [[ "${APP_MODE}" == "TESTBED" ]]; then
  echo "🚀 TESTBED direct launch on :${PORT}..."
  detach_exec --log "${IG_DATA_ROOT}/logs/agent_stdout.log" -- "${PY}" -u src/main.py
  AGENT_PID="${DETACH_PID}"
  echo "${AGENT_PID}" > "${IG_DATA_ROOT}/agent.pid"
else
  ./scripts/daemon_supervisor.sh
  SUP_PID="$(tr -d '[:space:]' < "${IG_DATA_ROOT}/supervisor.pid" 2>/dev/null || true)"
  echo "🚀 DAEMON SUPERVISOR DEPLOYED (PID ${SUP_PID:-?}) — polling :${PORT} for G5..."
fi

for _ in $(seq 1 60); do
  if curl -sf --max-time 3 "${HEALTH_URL}" -o /tmp/ig_health.json 2>/dev/null; then
    PHASE="$("${PY}" -c "import json; print(json.load(open('/tmp/ig_health.json')).get('system_state',{}).get('phase',''))" 2>/dev/null || echo "")"
    if [[ "${PHASE}" == "G5" ]]; then
      echo "✅ HTTP 200 + system_state.phase=G5 — APP_MODE=${APP_MODE} port=${PORT} scope=${IG_ACCOUNT_SCOPE:-masked}"
      exit 0
    fi
  fi
  sleep 5
done

echo "⚠️ Runtime launched; G5 not confirmed within 5 minutes (check ${SUP_LOG:-agent log})."
exit 5
