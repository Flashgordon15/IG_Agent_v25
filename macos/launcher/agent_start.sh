#!/bin/bash
# Isolated test gate → fresh agent interpreter → G5 → unified warm-up → GUI server (last).
# Idempotent when preceded by agent_kill.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"
# shellcheck source=lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

LOG="${IG_AGENT_ROOT}/logs/agent_start.log"
PYTEST_LOG="${IG_AGENT_ROOT}/logs/pytest_gate.log"
exec > >(tee -a "${LOG}") 2>&1

export_launch_env

log "========== agent_start begin mode=${APP_MODE} port=${PORT} =========="

if [[ "${APP_MODE}" == "DEMO" ]] && [[ "${LAUNCHER_SKIP_DEMO_RESET:-}" != "1" ]]; then
  log "[RESET] DEMO caches + daily P&L baseline"
  "${IG_AGENT_PY}" "${SCRIPT_DIR}/launcher_core.py" --phase reset || {
    log "ERROR: DEMO reset failed"
    exit 4
  }
fi

run_pytest_isolated() {
  if [[ "${LAUNCHER_SKIP_TESTS:-}" == "1" ]]; then
    log "[TEST] skipped (LAUNCHER_SKIP_TESTS=1)"
    return 0
  fi

  local gate_done="${IG_AGENT_ROOT}/logs/.pytest_gate.done"
  if [[ "${LAUNCHER_FORCE_TESTS:-}" != "1" ]] && [[ -f "${gate_done}" ]]; then
    local gate_age=$(( $(date +%s) - $(stat -f %m "${gate_done}" 2>/dev/null || echo 0) ))
    if (( gate_age < 3600 )); then
      log "[TEST] skipped — gate passed ${gate_age}s ago (LAUNCHER_FORCE_TESTS=1 to re-run)"
      return 0
    fi
  fi

  local timeout_sec="${LAUNCHER_TEST_TIMEOUT_SEC:-900}"
  local grace_after_pass_sec="${LAUNCHER_TEST_GRACE_SEC:-45}"
  local test_files=(
    tests/test_dual_core_execution.py tests/test_v31_telemetry.py
    tests/test_broker_epic_resolver.py tests/test_broker_reject_guard.py
    tests/test_live_canary_guards.py tests/test_live_canary_session.py
    tests/test_app_mode_session_lock.py tests/test_shutdown_and_health_identity.py
    tests/test_gui_session_attach.py tests/test_trade_pipeline_health.py
    tests/test_pipeline_governance.py tests/test_strategy_profile.py
    tests/test_strategy_selector.py tests/test_strategy_controller.py
    tests/test_strategy_transition.py tests/test_strategy_enforcement.py
    tests/test_hard_enforcement.py tests/test_adaptive_thresholds.py
    tests/test_strategy_performance_memory.py tests/test_regime_detection.py
    tests/test_regime_aware_selector.py tests/test_regime_risk_envelope.py
    tests/test_regime_sizing.py tests/test_daily_pnl_targeting.py
    tests/test_unified_execution.py tests/test_unified_routing_boot_warmup.py
    tests/test_strategy_governance.py tests/test_session_review.py
    tests/test_full_system_stress.py
  )

  : > "${PYTEST_LOG}"
  log "[TEST] isolated subprocess (hard timeout=${timeout_sec}s)"

  (
    cd "${IG_AGENT_ROOT}"
    exec env PYTHONPATH=src "${IG_AGENT_PY}" -m pytest "${test_files[@]}" -p no:anyio
  ) >> "${PYTEST_LOG}" 2>&1 &
  local py_pid=$!
  echo "${py_pid}" > "${IG_AGENT_ROOT}/logs/.pytest_pid"

  local elapsed=0
  while kill -0 "${py_pid}" 2>/dev/null; do
    if grep -qE '=+[[:space:]]+[0-9]+ passed' "${PYTEST_LOG}" 2>/dev/null; then
      local summary_line
      summary_line="$(grep -E '=+.*passed' "${PYTEST_LOG}" | tail -1 || true)"
      if echo "${summary_line}" | grep -qE '[1-9][0-9]* failed|[1-9][0-9]* error'; then
        kill -9 "${py_pid}" 2>/dev/null || true
        log "ERROR: pytest failures: ${summary_line}"
        return 1
      fi
      log "[TEST] pass summary — grace ${grace_after_pass_sec}s then forced cleanup"
      sleep "${grace_after_pass_sec}"
      if kill -0 "${py_pid}" 2>/dev/null; then
        kill -TERM "${py_pid}" 2>/dev/null || true
        sleep 8
        kill -0 "${py_pid}" 2>/dev/null && kill -9 "${py_pid}" 2>/dev/null || true
      fi
      pkill -9 -f "resource_tracker.*main" 2>/dev/null || true
      rm -f "${IG_AGENT_ROOT}/logs/.pytest_pid"
      touch "${IG_AGENT_ROOT}/logs/.pytest_gate.done"
      log "[TEST] passed (isolated + grace kill)"
      return 0
    fi
    if (( elapsed >= timeout_sec )); then
      kill -9 "${py_pid}" 2>/dev/null || true
      log "ERROR: pytest hard timeout ${timeout_sec}s"
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done

  wait "${py_pid}" 2>/dev/null || true
  if grep -qE '=+[[:space:]]+[0-9]+ passed' "${PYTEST_LOG}" 2>/dev/null; then
    touch "${IG_AGENT_ROOT}/logs/.pytest_gate.done"
    return 0
  fi
  log "ERROR: pytest exited without pass summary"
  return 1
}

wait_interpreter_stable() {
  local url="http://127.0.0.1:${PORT}/api/health"
  log "[AGENT] post-G5 health confirm (fast — G5 already verified)"
  for _ in 1 2 3; do
    if curl -sf --max-time 25 "${url}" -o /tmp/ig_agent_health.json 2>/dev/null; then
      log "[AGENT] post-G5 health OK"
      return 0
    fi
    sleep 3
  done
  log "WARN: post-G5 health slow — continuing anyway"
  return 0
}

wait_post_ready_execution() {
  local url="http://127.0.0.1:${PORT}/api/boot_status"
  local hl_url="http://127.0.0.1:${PORT}/api/health_light"
  local timeout_sec="${LAUNCHER_POST_READY_TIMEOUT_SEC:-90}"
  log "[AGENT] polling post-ready (boot_status + health_light, timeout=${timeout_sec}s)"
  local elapsed=0
  while (( elapsed < timeout_sec )); do
    if curl -sf --max-time 5 "${hl_url}" -o /tmp/ig_agent_health_light.json 2>/dev/null; then
      local sweep_alive sweep_count exec_active armed degraded
      read -r sweep_alive sweep_count exec_active armed degraded < <(
        "${IG_AGENT_PY}" -c "
import json
d=json.load(open('/tmp/ig_agent_health_light.json'))
rs=d.get('routing_state') or {}
print(
    int(bool(d.get('stacked_sweep_alive'))),
    int(d.get('rotation_sweep_count') or 0),
    int(bool(d.get('execution_loop_active'))),
    int(rs.get('armed') or 0),
    int(bool(rs.get('degraded'))),
)
" 2>/dev/null || echo "0 0 0 0 1"
      )
      log "[AGENT] post-ready stacked_alive=${sweep_alive} sweep=${sweep_count} exec_active=${exec_active} routes_armed=${armed} degraded=${degraded}"
      if [[ "${sweep_alive}" == "1" ]] && [[ "${exec_active}" == "1" ]]; then
        log "[AGENT] post-ready execution plane confirmed"
        return 0
      fi
    fi
    if curl -sf --max-time 3 "${url}" -o /tmp/ig_agent_boot_status.json 2>/dev/null; then
      local trade_ready
      trade_ready="$("${IG_AGENT_PY}" -c "import json; print(int(json.load(open('/tmp/ig_agent_boot_status.json')).get('trade_ready', False)))" 2>/dev/null || echo 0)"
      if [[ "${trade_ready}" == "1" ]]; then
        log "[AGENT] trade_ready confirmed via boot_status"
        return 0
      fi
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  log "WARN: post-ready execution not confirmed within ${timeout_sec}s — continuing"
  return 0
}

wait_g5() {
  local url="http://127.0.0.1:${PORT}/api/health"
  log "[AGENT] polling G5"
  for _ in $(seq 1 72); do
    if curl -sf --max-time 25 "${url}" -o /tmp/ig_agent_health.json 2>/dev/null; then
      PHASE="$("${IG_AGENT_PY}" -c "
import json
d=json.load(open('/tmp/ig_agent_health.json'))
ss = d.get('system_state') or d.get('boot_metrics', {}).get('system_state') or {}
print(ss.get('phase', ''))
" 2>/dev/null || echo "")"
      READY="$("${IG_AGENT_PY}" -c "
import json
d=json.load(open('/tmp/ig_agent_health.json'))
print(d.get('boot_metrics', {}).get('ready', d.get('ready', d.get('ok', ''))))
" 2>/dev/null || echo "")"
      STATUS="$("${IG_AGENT_PY}" -c "
import json
d=json.load(open('/tmp/ig_agent_health.json'))
print(str(d.get('status') or '').upper())
" 2>/dev/null || echo "")"
      log "[AGENT] phase=${PHASE} ready=${READY} status=${STATUS}"
      if [[ "${PHASE}" == "G5" ]] || [[ "${PHASE}" == "READY" && "${READY}" == "True" ]]; then
        return 0
      fi
      if [[ "${STATUS}" == "OPERATIONAL" && "${READY}" == "True" ]]; then
        return 0
      fi
    fi
    sleep 5
  done
  return 1
}

start_gui_server() {
  if [[ "${LAUNCHER_SKIP_GUI_SERVER:-}" == "1" ]]; then
    log "[GUI] server skipped (LAUNCHER_SKIP_GUI_SERVER=1)"
    return 0
  fi
  local dist="${IG_AGENT_ROOT}/dashboard/dist/index.html"
  if [[ -f "${dist}" ]]; then
    log "[GUI] dashboard dist served by agent on :${PORT}"
    return 0
  fi
  local pkg="${IG_AGENT_ROOT}/dashboard/package.json"
  if [[ ! -f "${pkg}" ]]; then
    log "[GUI] no dashboard/package.json — skip dev server"
    return 0
  fi
  if [[ "${LAUNCHER_SKIP_NPM_DEV:-}" == "1" ]]; then
    log "[GUI] npm dev skipped (LAUNCHER_SKIP_NPM_DEV=1)"
    return 0
  fi
  log "[GUI] starting Vite dev server (background :5173)"
  (
    cd "${IG_AGENT_ROOT}/dashboard"
    nohup npm run dev >> "${IG_AGENT_ROOT}/logs/vite_dev.log" 2>&1 &
    echo $! > "${IG_AGENT_ROOT}/logs/.gui_server.pid"
  )
  sleep 3
  log "[GUI] Vite pid=$(cat "${IG_AGENT_ROOT}/logs/.gui_server.pid" 2>/dev/null || echo unknown)"
}

log "[PREFLIGHT]"
"${IG_AGENT_PY}" -m runtime.session_lock preflight \
  --mode "${APP_MODE}" --port "${PORT}" --config "${IG_AGENT_CONFIG}"

log "[TEST]"
if ! run_pytest_isolated; then
  alert_launcher "Pytest gate failed — see logs/pytest_gate.log"
  exit 5
fi

log "[AGENT] fresh interpreter via daemon_supervisor"
SUP_LOG="${IG_DATA_ROOT}/logs/supervisor.log"
if [[ "${APP_MODE}" == "TESTBED" ]]; then
  nohup env PYTHONPATH=src "${IG_AGENT_PY}" -u "${IG_AGENT_ROOT}/src/main.py" \
    >> "${IG_DATA_ROOT}/logs/agent_stdout.log" 2>&1 &
  echo $! > "${IG_DATA_ROOT}/agent.pid"
else
  export DAEMON_SUPERVISOR_REDIRECT=1
  nohup "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" >> "${SUP_LOG}" 2>&1 &
  echo $! > "${IG_DATA_ROOT}/supervisor.pid"
fi

if ! wait_g5; then
  alert_launcher "G5 not reached within 6 minutes"
  exit 6
fi

if ! wait_interpreter_stable; then
  log "WARN: interpreter stability check incomplete — continuing"
fi

if [[ "${LAUNCHER_SKIP_POST_READY_WAIT:-}" != "1" ]]; then
  wait_post_ready_execution || true
fi

log "[WARMUP] unified execution route cache"
ROUTE_COUNT="$("${IG_AGENT_PY}" -c "
from api.gui_status import warm_unified_execution_route_cache
print(warm_unified_execution_route_cache())
" 2>/dev/null || echo "0")"
log "[WARMUP] routes=${ROUTE_COUNT} (feed hub arms inside agent post-G5 — not modified here)"

start_gui_server

log "agent_start complete"
exit 0
