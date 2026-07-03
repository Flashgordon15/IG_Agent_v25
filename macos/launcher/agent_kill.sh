#!/bin/bash
# Kill ALL IG Agent processes, free port 8080, remove PID/lock files, clear caches.
# Anti-zombie protocol: mark_manual_stop → stop supervisors → graceful TERM → port free.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"
# shellcheck source=lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

export_launch_env

PORT="${IG_API_PORT}"
COCKPIT_PORT="${IG_COCKPIT_PORT:-8787}"
LOG="${IG_AGENT_ROOT}/logs/agent_kill.log"
exec > >(tee -a "${LOG}") 2>&1

log "========== agent_kill begin (port=${PORT}) =========="
launcher_status_init
launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Engaging manual stop hold" 1 9

# A. Hold — block watchdog/supervisor auto-restart (MUST be first).
"${IG_AGENT_PY}" -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='agent_kill')" \
  2>/dev/null || log "WARN: mark_manual_stop skipped"

# B. Stop supervisors before touching main.py (prevents respawn during TERM).
launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Stopping daemon supervisors" 1 9
stop_supervisor_stack

kill_pidfile() {
  local f="$1"
  if [[ -f "${f}" ]]; then
    local pid
    pid="$(tr -d '[:space:]' < "${f}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill -TERM "${pid}" 2>/dev/null || true
      sleep 2
      kill -0 "${pid}" 2>/dev/null && kill -9 "${pid}" 2>/dev/null || true
      log "killed pid ${pid} from ${f}"
    fi
    rm -f "${f}"
  fi
}

launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Stopping supervised agent processes" 1 9

if [[ -x "${IG_AGENT_ROOT}/scripts/stop.sh" ]]; then
  "${IG_AGENT_ROOT}/scripts/stop.sh" --mode "${APP_MODE}" 2>/dev/null || log "WARN: stop.sh (no active session)"
fi

for pf in \
  "${IG_AGENT_ROOT}/logs/.pytest_pid" \
  "${IG_AGENT_ROOT}/logs/vite.pid" \
  "${IG_AGENT_ROOT}/logs/.gui_server.pid"; do
  kill_pidfile "${pf}"
done

log "TERM — IG Agent process families"
launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Sending graceful TERM to agent PIDs" 1 9
# Never kill the active launcher supervisor — agent_kill runs inside igagent_launcher.sh.
kill_pattern "-TERM" \
  "${IG_AGENT_ROOT}/src/main.py" \
  "${IG_AGENT_ROOT}/scripts/watchdog.sh" \
  "${IG_AGENT_ROOT}/scripts/start.sh" \
  "pytest.*${IG_AGENT_ROOT}" \
  "multi_feed_hub" \
  "resource_tracker" \
  "npm run dev" \
  "vite.*dashboard"

for i in 1 2 3 4 5; do
  launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Waiting for graceful exit (${i}/5)" 1 9
  sleep 3
done

log "KILL — survivors"
launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Escalating stuck processes" 1 9
kill_pattern "-9" \
  "${IG_AGENT_ROOT}/src/main.py" \
  "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" \
  "${IG_AGENT_ROOT}/scripts/watchdog.sh" \
  "pytest.*${IG_AGENT_ROOT}" \
  "multi_feed_hub" \
  "npm run dev"

# Port 8080, Flight Deck 8787 (and vite 5173 if used)
launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Freeing ports ${PORT}, ${COCKPIT_PORT} and 5173" 1 9
for p in "${PORT}" "${COCKPIT_PORT}" 5173; do
  PORT_PIDS="$(pids_on_port "${p}")"
  if [[ -n "${PORT_PIDS}" ]]; then
    log "port ${p} bound — escalating: ${PORT_PIDS}"
    echo "${PORT_PIDS}" | xargs kill -TERM 2>/dev/null || true
    sleep 8
    PORT_PIDS="$(pids_on_port "${p}")"
    [[ -n "${PORT_PIDS}" ]] && echo "${PORT_PIDS}" | xargs kill -9 2>/dev/null || true
  fi
done

log "Purging bytecode, locks, stale PID files"
launcher_status_set "shutdown" "Stage 1 — Clean shutdown" "Clearing locks and bytecode cache" 1 9
find "${IG_AGENT_ROOT}/src" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "${IG_AGENT_ROOT}/src" -name '*.pyc' -delete 2>/dev/null || true
rm -f "${IG_AGENT_ROOT}/src/data/.ig_agent_v29.lock"
if [[ -f "${IG_AGENT_ROOT}/emergency_stop.lock" ]]; then
  rm -f "${IG_AGENT_ROOT}/emergency_stop.lock"
  log "cleared emergency_stop.lock (clean launcher restart)"
fi
rm -f "${IG_AGENT_ROOT}/src/data/v31-production/agent.pid" \
      "${IG_AGENT_ROOT}/src/data/v31-production/supervisor.pid" \
      "${IG_AGENT_ROOT}/logs/.pytest_pid" \
      "${IG_AGENT_ROOT}/logs/.pytest_exit" \
      "${IG_AGENT_ROOT}/logs/vite.pid" \
      "${IG_AGENT_ROOT}/logs/.gui_server.pid" 2>/dev/null || true
rm -rf "${IG_AGENT_ROOT}/logs/.launcher.lock.d" 2>/dev/null || true

DATA_ROOT="$(resolve_data_root 2>/dev/null || echo "")"
if [[ -n "${DATA_ROOT}" ]]; then
  rm -f "${DATA_ROOT}/agent.pid" "${DATA_ROOT}/supervisor.pid" "${DATA_ROOT}/port_hold.pid" 2>/dev/null || true
fi

# Final supervisor sweep (belt-and-braces).
stop_supervisor_stack

if [[ -n "$(pids_on_port "${PORT}")" ]]; then
  log "ERROR: port ${PORT} still bound"
  launcher_status_fail "Shutdown failed" "Port ${PORT} still bound after agent_kill" 1
  alert_launcher "Port ${PORT} still bound after agent_kill"
  exit 1
fi

if ! verify_clean_slate; then
  log "WARN: clean slate blockers remain — final escalation"
  kill_pattern "-9" \
    "${IG_AGENT_ROOT}/src/main.py" \
    "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh"
  stop_supervisor_stack
  for p in "${PORT}" "${COCKPIT_PORT}" 5173; do
    PORT_PIDS="$(pids_on_port "${p}")"
    [[ -n "${PORT_PIDS}" ]] && echo "${PORT_PIDS}" | xargs kill -9 2>/dev/null || true
  done
  sleep 2
fi
if ! verify_clean_slate; then
  log "ERROR: clean slate verification failed after escalation"
  launcher_status_fail "Shutdown failed" "Zombie processes or port still bound" 1
  alert_launcher "agent_kill: clean slate failed — check main.py / supervisor / port ${PORT}"
  exit 1
fi

launcher_status_set "shutdown" "Stage 1 complete" "Port ${PORT} free — ready to load" 1 9
log "agent_kill complete — port ${PORT} free"
exit 0
