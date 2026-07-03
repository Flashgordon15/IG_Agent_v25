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

if ! acquire_launcher_lock; then
  log "ERROR: another launcher holds logs/.launcher.lock — exit or set LAUNCHER_WAIT_FOR_LOCK=1"
  launcher_status_fail "Launch blocked" "Another launch is in progress" 0
  exit 3
fi

log "========== agent_start begin mode=${APP_MODE} port=${PORT} =========="

# Desktop shortcut: full assessment every launch (no pytest cache skip).
if [[ "${LAUNCHER_DESKTOP:-}" == "1" ]]; then
  rm -f "${IG_AGENT_ROOT}/logs/.pytest_gate.done"
  export LAUNCHER_FORCE_TESTS=1
fi

launcher_status_set "preflight" "Stage 2 — Preflight" "Mode ${APP_MODE} on port ${PORT}" 2 9

if [[ "${APP_MODE}" == "DEMO" ]] && [[ "${LAUNCHER_SKIP_DEMO_RESET:-}" != "1" ]]; then
  log "[RESET] DEMO caches + daily P&L baseline"
  launcher_status_set "preflight" "Stage 2 — Preflight" "Resetting DEMO caches and P&L baseline" 2 9
  "${IG_AGENT_PY}" "${SCRIPT_DIR}/launcher_core.py" --phase reset || {
    log "ERROR: DEMO reset failed"
    launcher_status_fail "Preflight failed" "DEMO reset failed" 2
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

  # Fast smoke gate (~30–60s, 3 core files). Full suite: LAUNCHER_TEST_SUITE=full.
  local pytest_k=""
  local timeout_sec="${LAUNCHER_TEST_TIMEOUT_SEC:-180}"
  local test_files=(
    tests/test_app_mode_session_lock.py
    tests/test_boot_orchestrator.py
    tests/test_shutdown_and_health_identity.py
  )
  pytest_k='not test_health_identity_fields_demo_live_testbed'
  pytest_k+=' and not test_health_endpoint_includes_identity_fields'
  if [[ "${LAUNCHER_TEST_SUITE:-smoke}" == "full" ]]; then
    timeout_sec="${LAUNCHER_TEST_TIMEOUT_SEC:-900}"
    pytest_k=""
    test_files=(
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
    )
  fi

  : > "${PYTEST_LOG}"
  log "[TEST] smoke gate (${#test_files[@]} files, timeout=${timeout_sec}s)"
  launcher_status_set "tests" "Stage 3 — Assessment" "Running smoke tests (${#test_files[@]} files, ~1 min)" 3 9

  local pytest_cmd=( -m pytest "${test_files[@]}" -p no:anyio -q )
  if [[ -n "${pytest_k}" ]]; then
    pytest_cmd+=( -k "${pytest_k}" )
  fi

  (
    cd "${IG_AGENT_ROOT}"
    exec env PYTHONUNBUFFERED=1 PYTHONPATH=src IG_AGENT_PYTEST=1 \
      IG_AGENT_CONFIG=config/config_v31.json "${IG_AGENT_PY}" -u "${pytest_cmd[@]}"
  ) >> "${PYTEST_LOG}" 2>&1 &
  local py_pid=$!
  echo "${py_pid}" > "${IG_AGENT_ROOT}/logs/.pytest_pid"

  local elapsed=0 exit_code=0
  while kill -0 "${py_pid}" 2>/dev/null; do
    if (( elapsed >= timeout_sec )); then
      kill -9 "${py_pid}" 2>/dev/null || true
      log "ERROR: pytest hard timeout ${timeout_sec}s"
      rm -f "${IG_AGENT_ROOT}/logs/.pytest_pid"
      launcher_status_fail "Assessment failed" "Pytest gate timed out after ${timeout_sec}s" 3
      return 1
    fi
    if (( elapsed % 10 == 0 )); then
      launcher_status_set "tests" "Stage 3 — Assessment" "Smoke tests running… (${elapsed}s)" 3 9
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  wait "${py_pid}" 2>/dev/null || exit_code=$?
  rm -f "${IG_AGENT_ROOT}/logs/.pytest_pid"
  pkill -9 -f "resource_tracker.*main" 2>/dev/null || true

  local summary_line
  summary_line="$(grep -E '=+.*passed' "${PYTEST_LOG}" | tail -1 || true)"
  if (( exit_code == 0 )); then
    touch "${IG_AGENT_ROOT}/logs/.pytest_gate.done"
    log "[TEST] passed (exit 0) ${summary_line}"
    launcher_status_set "tests" "Stage 3 complete" "${summary_line:-Smoke tests passed}" 3 9
    return 0
  fi
  log "ERROR: pytest exit ${exit_code} — ${summary_line:-see logs/pytest_gate.log}"
  launcher_status_fail "Assessment failed" "${summary_line:-pytest exit ${exit_code}}" 3
  return 1
}

wait_interpreter_stable() {
  local hl_url="http://127.0.0.1:${PORT}/api/health_light"
  local fast_url="http://127.0.0.1:${PORT}/health"
  log "[AGENT] post-G5 health confirm (health_light fast path — avoids heavy /api/health)"
  for _ in 1 2 3 4 5 6 8 10; do
    if curl -sf --max-time 5 -H "User-Agent: IG-Agent-Launcher/31" "${hl_url}" \
      -o /tmp/ig_agent_health_light.json 2>/dev/null; then
      log "[AGENT] post-G5 health_light OK"
      return 0
    fi
    if curl -sf --max-time 3 -H "User-Agent: IG-Agent-Launcher/31" "${fast_url}" \
      -o /dev/null 2>/dev/null; then
      log "[AGENT] post-G5 /health OK"
      return 0
    fi
    sleep 2
  done
  log "WARN: post-G5 health_light slow — continuing anyway (agent may still be hydrating)"
  return 0
}

wait_post_ready_execution() {
  local url="http://127.0.0.1:${PORT}/api/boot_status"
  local hl_url="http://127.0.0.1:${PORT}/api/health_light"
  local timeout_sec="${LAUNCHER_POST_READY_TIMEOUT_SEC:-120}"
  log "[AGENT] polling post-ready (boot_status + health_light, timeout=${timeout_sec}s)"
  launcher_status_set "post_ready" "Stage 6 — Execution plane" "Confirming trade-ready routing…" 6 9
  local elapsed=0
  while (( elapsed < timeout_sec )); do
    local boot_tier
    boot_tier="$("${IG_AGENT_ROOT}/scripts/boot_acceptance.sh" --tier-only 2>/dev/null || echo red)"
    if curl -sf --max-time 5 "${hl_url}" -o /tmp/ig_agent_health_light.json 2>/dev/null; then
      curl -sf --max-time 3 "${url}" -o /tmp/ig_agent_boot_status.json 2>/dev/null || true
      local sweep_alive sweep_count exec_active armed degraded accept reason
      read -r sweep_alive sweep_count exec_active armed degraded < <(
        env PYTHONPATH="${IG_AGENT_ROOT}/src" "${IG_AGENT_PY}" -c "
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
      read -r accept reason < <(
        env PYTHONPATH="${IG_AGENT_ROOT}/src" "${IG_AGENT_PY}" -c "
import json
from cockpit.launcher_post_ready import post_ready_execution_acceptable
hl=json.load(open('/tmp/ig_agent_health_light.json'))
boot={}
try:
    boot=json.load(open('/tmp/ig_agent_boot_status.json'))
except Exception:
    pass
ok, why = post_ready_execution_acceptable(
    health_light=hl, boot_status=boot, boot_tier='${boot_tier}'
)
print(int(ok), why)
" 2>/dev/null || echo "0 pending"
      )
      if [[ "${accept}" == "0" ]] && [[ "${boot_tier}" == "amber" || "${boot_tier}" == "green" ]]; then
        accept=1
        reason="tier_${boot_tier}_fallback"
      fi
      log "[AGENT] post-ready tier=${boot_tier} stacked_alive=${sweep_alive} sweep=${sweep_count} exec_active=${exec_active} routes_armed=${armed} degraded=${degraded} accept=${accept} reason=${reason}"
      if [[ "${accept}" == "1" ]]; then
        log "[AGENT] post-ready execution plane confirmed (${reason})"
        launcher_status_set "post_ready" "Stage 6 complete" "Stage 6 complete — ${reason}" 6 9 "" "${boot_tier}"
        return 0
      fi
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  log "WARN: post-ready execution not confirmed within ${timeout_sec}s — continuing (amber/degraded ok)"
  launcher_status_set "post_ready" "Stage 6 complete" "Stage 6 complete — timeout (degraded)" 6 9 "" "amber"
  return 0
}

restart_fresh_agent() {
  log "[AGENT] restarting fresh interpreter after stuck-port escalate"
  export DAEMON_SUPERVISOR_REDIRECT=1
  rm -f "${IG_DATA_ROOT}/supervisor.pid"
  nohup "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" >> "${SUP_LOG}" 2>&1 &
  wait_for_supervisor_pid || true
}

wait_g5() {
  local url="http://127.0.0.1:${PORT}/api/health"
  local hl_url="http://127.0.0.1:${PORT}/api/health_light"
  local fast_url="http://127.0.0.1:${PORT}/health"
  log "[AGENT] polling G5"
  launcher_status_set "g5" "Stage 5 — Boot gates" "Waiting for G5 operational…" 5 9
  local g5_wait="${LAUNCHER_G5_WAIT_SEC:-900}"
  local g5_polls=$((g5_wait / 5))
  if (( g5_polls < 12 )); then g5_polls=12; fi
  local bind_deadline=$(( $(date +%s) + ${LAUNCHER_BIND_DEADLINE_SEC:-90} ))
  local stuck_escalate_sec="${LAUNCHER_G5_STUCK_ESCALATE_SEC:-20}"
  local hydration_pass_sec="${LAUNCHER_G5_HYDRATION_PASS_SEC:-45}"
  local health_seen=0
  local port_live=0
  local stuck_escalated=0
  local bind_wait_start
  bind_wait_start="$(date +%s)"
  for i in $(seq 1 "${g5_polls}"); do
    local tier port_pids bound_sec
    port_pids="$(pids_on_port "${PORT}")"
    if port_accepts_tcp "${PORT}"; then
      port_live=1
      health_seen=1
    fi
    if http_any_alive "${PORT}"; then
      health_seen=1
      port_live=1
    fi
    tier="$("${IG_AGENT_ROOT}/scripts/boot_acceptance.sh" --tier-only 2>/dev/null || echo red)"
    if [[ "${tier}" != "red" ]]; then
      health_seen=1
    fi
    if (( port_live == 1 )) && [[ "${tier}" == "red" ]]; then
      launcher_status_set "g5" "Stage 5 — Boot gates" "Agent bound — hydration in progress…" 5 9 "" "amber"
      bound_hydration_sec=$(( $(date +%s) - bind_wait_start ))
      if (( bound_hydration_sec >= hydration_pass_sec )); then
        log "[AGENT] G5 hydration pass (${bound_hydration_sec}s bound — continuing boot in background)"
        launcher_status_set "g5" "Stage 5 complete" "Agent bound — boot gates hydrating in background" 5 9 "" "amber"
        return 0
      fi
    fi
    if (( health_seen == 0 )) && [[ -n "${port_pids}" ]]; then
      bound_sec=$(( $(date +%s) - bind_wait_start ))
      if (( bound_sec >= stuck_escalate_sec )) && (( stuck_escalated == 0 )); then
        if port_accepts_tcp "${PORT}"; then
          log "WARN: :${PORT} TCP live but HTTP slow ${bound_sec}s — holding (hydration)"
          health_seen=1
          port_live=1
        else
          log "WARN: :${PORT} bound (pids=${port_pids}) but TCP dead ${bound_sec}s — escalating"
          launcher_status_set "g5" "Stage 5 — Boot gates" "Recycling unresponsive agent on :${PORT}…" 5 9
          kill_port_listeners "${PORT}"
          stuck_escalated=1
          port_live=0
          if [[ "${REUSE_EXISTING_AGENT:-}" != "1" ]]; then
            wait_port_free "${PORT}" 15 || true
            restart_fresh_agent
          fi
          bind_wait_start="$(date +%s)"
          bind_deadline=$(( $(date +%s) + ${LAUNCHER_BIND_DEADLINE_SEC:-90} ))
          sleep 5
          continue
        fi
      fi
    fi
    if (( port_live == 0 )) && (( $(date +%s) > bind_deadline )); then
      port_pids="$(pids_on_port "${PORT}")"
      if [[ -n "${port_pids}" ]]; then
        log "ERROR: :${PORT} bound (pids=${port_pids}) but not accepting TCP within ${LAUNCHER_BIND_DEADLINE_SEC:-90}s"
        launcher_status_fail "Boot failed" "Port :${PORT} bound but not accepting connections" 5
      else
        log "ERROR: agent never bound :${PORT} within ${LAUNCHER_BIND_DEADLINE_SEC:-90}s"
        launcher_status_fail "Boot failed" "Agent never bound :${PORT}" 5
      fi
      return 1
    fi
    if [[ "${tier}" == "green" ]]; then
      log "[AGENT] boot tier=green"
      launcher_status_set "g5" "Stage 5 complete" "G5 operational" 5 9 "" "green"
      return 0
    fi
    if [[ "${tier}" == "amber" ]]; then
      log "[AGENT] boot tier=amber (degraded — API live, hydration continuing)"
      launcher_status_set "g5" "Stage 5 complete" "Agent ready (degraded — IG/feeds hydrating)" 5 9 "" "amber"
      return 0
    fi
    if curl -sf --max-time 3 -H "User-Agent: IG-Agent-Launcher/31" "${fast_url}" -o /dev/null 2>/dev/null \
      || curl -sf --max-time 3 -H "User-Agent: IG-Agent-Launcher/31" "${hl_url}" -o /dev/null 2>/dev/null \
      || curl -sf --max-time 8 -H "User-Agent: IG-Agent-Launcher/31" "${url}" -o /tmp/ig_agent_health.json 2>/dev/null; then
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
      log "[AGENT] phase=${PHASE} ready=${READY} status=${STATUS} tier=${tier}"
      launcher_status_set "g5" "Stage 5 — Boot gates" "Phase ${PHASE:-starting} · status ${STATUS:-…}" 5 9
    fi
    sleep 5
  done
  launcher_status_fail "Boot failed" "G5 not reached within $((g5_wait / 60)) minutes" 5
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

launch_electron_gui() {
  if [[ "${LAUNCHER_SKIP_ELECTRON_GUI:-}" == "1" ]]; then
    log "[ELECTRON] skipped (LAUNCHER_SKIP_ELECTRON_GUI=1)"
    return 0
  fi
  local launcher="${SCRIPT_DIR}/launch_electron_gui.sh"
  if [[ ! -x "${launcher}" ]]; then
    chmod +x "${launcher}" 2>/dev/null || true
  fi
  if [[ -x "${launcher}" ]]; then
    log "[ELECTRON] opening native Apex window"
    /bin/bash "${launcher}" || log "[ELECTRON] WARN: GUI launch failed — see logs/electron_gui.log"
  else
    log "[ELECTRON] WARN: launch_electron_gui.sh missing"
  fi
}

log "[PREFLIGHT]"
launcher_status_set "preflight" "Stage 2 — Preflight" "Session lock and port check" 2 9

# Ensure no zombie supervisors/sessions from a prior unclean exit.
if pgrep -f "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" >/dev/null 2>&1; then
  log "[PREFLIGHT] stale supervisor detected — stopping before fresh start"
  stop_supervisor_stack
fi
# Keep manual_stop from agent_kill until startup completes — blocks launchd watchdog race.

"${IG_AGENT_PY}" -m runtime.session_lock preflight \
  --mode "${APP_MODE}" --port "${PORT}" --config "${IG_AGENT_CONFIG}"

log "[TEST]"
if ! run_pytest_isolated; then
  alert_launcher "Pytest gate failed — see logs/pytest_gate.log"
  exit 5
fi

log "[AGENT] fresh interpreter via daemon_supervisor"
launcher_status_set "agent_boot" "Stage 4 — Agent start" "Launching fresh interpreter" 4 9
SUP_LOG="${IG_DATA_ROOT}/logs/supervisor.log"
ensure_agent_port_ready
if [[ "${REUSE_EXISTING_AGENT:-}" == "1" ]]; then
  log "[AGENT] skipping daemon_supervisor — healthy agent already serving :${PORT}"
  launcher_status_set "agent_boot" "Stage 4 complete" "Reusing healthy agent on :${PORT}" 4 9
else
  if [[ "${APP_MODE}" == "TESTBED" ]]; then
    "${IG_AGENT_PY}" -c "from system.shutdown_cleanup import clear_manual_stop; clear_manual_stop()" \
      2>/dev/null || true
    nohup env PYTHONPATH=src "${IG_AGENT_PY}" -u "${IG_AGENT_ROOT}/src/main.py" \
      >> "${IG_DATA_ROOT}/logs/agent_stdout.log" 2>&1 &
    echo $! > "${IG_DATA_ROOT}/agent.pid"
  else
    # Release hold only when spawning supervisor — watchdog defers via supervisor_managed().
    "${IG_AGENT_PY}" -c "from system.shutdown_cleanup import clear_manual_stop; clear_manual_stop()" \
      2>/dev/null || true
    export DAEMON_SUPERVISOR_REDIRECT=1
    rm -f "${IG_DATA_ROOT}/supervisor.pid"
    nohup "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" >> "${SUP_LOG}" 2>&1 &
    wait_for_supervisor_pid || log "WARN: supervisor pid not confirmed"
  fi
fi

if ! wait_g5; then
  alert_launcher "G5 not reached within $(( ${LAUNCHER_G5_WAIT_SEC:-900} / 60 )) minutes"
  exit 6
fi

if ! wait_interpreter_stable; then
  log "WARN: interpreter stability check incomplete — continuing"
fi

if [[ "${LAUNCHER_SKIP_POST_READY_WAIT:-}" != "1" ]]; then
  wait_post_ready_execution || true
fi

log "[WARMUP] unified execution route cache"
launcher_status_set "warmup" "Stage 7 — Warm-up" "Priming unified execution routes" 7 9
ROUTE_COUNT="$("${IG_AGENT_PY}" -c "
from api.gui_status import warm_unified_execution_route_cache
print(warm_unified_execution_route_cache())
" 2>/dev/null || echo "0")"
log "[WARMUP] routes=${ROUTE_COUNT} (feed hub arms inside agent post-G5 — not modified here)"
launcher_status_set "warmup" "Stage 7 complete" "Routes warmed (${ROUTE_COUNT})" 7 9

start_gui_server

launcher_status_set "agent_boot" "Stage 4–7 complete" "Agent loaded and warmed" 7 9
log "agent_start complete"
launch_electron_gui
exit 0
