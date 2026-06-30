#!/bin/bash
# Kill ALL IG Agent processes, free port 8080, remove PID/lock files, clear caches.
# Idempotent — safe to run repeatedly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"
# shellcheck source=lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

PORT="${IG_API_PORT}"
LOG="${IG_AGENT_ROOT}/logs/agent_kill.log"
exec > >(tee -a "${LOG}") 2>&1

log "========== agent_kill begin (port=${PORT}) =========="

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

"${IG_AGENT_PY}" -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='agent_kill')" \
  2>/dev/null || log "WARN: mark_manual_stop skipped"

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
kill_pattern "-TERM" \
  "${IG_AGENT_ROOT}/src/main.py" \
  "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" \
  "${IG_AGENT_ROOT}/scripts/watchdog.sh" \
  "${IG_AGENT_ROOT}/macos/launcher/launch_agent.sh" \
  "${IG_AGENT_ROOT}/macos/launcher/igagent_launcher.sh" \
  "${IG_AGENT_ROOT}/macos/launcher/agent_start.sh" \
  "${IG_AGENT_ROOT}/macos/launcher/IGAgentSupervisor" \
  "${IG_AGENT_ROOT}/scripts/start.sh" \
  "pytest.*${IG_AGENT_ROOT}" \
  "multi_feed_hub" \
  "resource_tracker" \
  "npm run dev" \
  "vite.*dashboard"

sleep 15

log "KILL — survivors"
kill_pattern "-9" \
  "${IG_AGENT_ROOT}/src/main.py" \
  "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" \
  "${IG_AGENT_ROOT}/scripts/watchdog.sh" \
  "pytest.*${IG_AGENT_ROOT}" \
  "multi_feed_hub" \
  "npm run dev"

# Port 8080 (and vite 5173 if used)
for p in "${PORT}" 5173; do
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
# Preserve logs/.pytest_gate.done — enables fast re-launch within 1h (see agent_start.sh)

DATA_ROOT="$(resolve_data_root 2>/dev/null || echo "")"
if [[ -n "${DATA_ROOT}" ]]; then
  rm -f "${DATA_ROOT}/agent.pid" "${DATA_ROOT}/supervisor.pid" 2>/dev/null || true
fi

if [[ -n "$(pids_on_port "${PORT}")" ]]; then
  log "ERROR: port ${PORT} still bound"
  alert_launcher "Port ${PORT} still bound after agent_kill"
  exit 1
fi

log "agent_kill complete — port ${PORT} free"
exit 0
