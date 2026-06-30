#!/bin/bash
# Launch IG Cockpit (Tauri) after verify — production-hardened supervisor entry.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"

PORT="${IG_API_PORT:-8080}"
COCKPIT="${IG_AGENT_ROOT}/gui/ig_cockpit"
LOG="${IG_AGENT_ROOT}/logs/cockpit_launch.log"
API_URL="http://127.0.0.1:${PORT}"
PID_FILE="${IG_AGENT_ROOT}/logs/.cockpit.pid"

mkdir -p "${IG_AGENT_ROOT}/logs"
exec >> "${LOG}" 2>&1

log "========== agent_gui cockpit launch port=${PORT} =========="

# Kill stale cockpit dev server if present
if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
    log "Stopping stale cockpit pid=${old_pid}"
    kill -TERM "${old_pid}" 2>/dev/null || true
    sleep 2
  fi
  rm -f "${PID_FILE}"
fi

if ! curl -sf --max-time 30 "${API_URL}/api/gui_status" -o /tmp/ig_cockpit_gui.json; then
  log "ERROR: /api/gui_status not ready — aborting cockpit launch"
  exit 1
fi

# Verify required gui_status fields (matches launcher verify contract)
if ! "${IG_AGENT_PY}" -c "
import json, sys
required = [
  'strategy_selector_advice', 'strategy_controller_decisions',
  'strategy_governance', 'unified_execution_route',
  'hard_enforcement_decisions', 'trade_pipeline_health',
]
d = json.load(open('/tmp/ig_cockpit_gui.json'))
missing = [f for f in required if f not in d]
sys.exit(1 if missing else 0)
" 2>/dev/null; then
  log "WARN: gui_status incomplete — launching anyway (cockpit splash will wait)"
fi

export IG_AGENT_API_URL="${API_URL}"
export VITE_IG_AGENT_API_URL="${API_URL}"

APP_RELEASE="${COCKPIT}/src-tauri/target/release/bundle/macos/IG Cockpit.app"
APP_DEBUG="${COCKPIT}/src-tauri/target/debug/bundle/macos/IG Cockpit.app"
SUP_RELEASE="${COCKPIT}/src-tauri/target/release/ig-cockpit"
SUP_DEBUG="${COCKPIT}/src-tauri/target/debug/ig-cockpit"

launch_env() {
  env IG_AGENT_API_URL="${API_URL}" VITE_IG_AGENT_API_URL="${API_URL}" "$@"
}

open_browser_fallback() {
  log "Fallback: browser dashboard"
  "${IG_AGENT_PY}" "${SCRIPT_DIR}/launcher_core.py" --phase gui --port "${PORT}"
}

if [[ -d "${APP_RELEASE}" ]]; then
  log "Opening release bundle: ${APP_RELEASE}"
  launch_env open "${APP_RELEASE}"
elif [[ -d "${APP_DEBUG}" ]]; then
  log "Opening debug bundle: ${APP_DEBUG}"
  launch_env open "${APP_DEBUG}"
elif [[ -x "${SUP_RELEASE}" ]]; then
  log "Launching release binary: ${SUP_RELEASE}"
  launch_env nohup "${SUP_RELEASE}" >> "${LOG}" 2>&1 &
  echo $! > "${PID_FILE}"
elif [[ -x "${SUP_DEBUG}" ]]; then
  log "Launching debug binary: ${SUP_DEBUG}"
  launch_env nohup "${SUP_DEBUG}" >> "${LOG}" 2>&1 &
  echo $! > "${PID_FILE}"
elif [[ -f "${COCKPIT}/package.json" ]] && command -v npm >/dev/null 2>&1; then
  log "Starting tauri:dev (background)"
  (
    cd "${COCKPIT}"
    launch_env nohup npm run tauri:dev >> "${LOG}" 2>&1 &
    echo $! > "${PID_FILE}"
  )
else
  log "WARN: No Tauri cockpit found — browser fallback"
  open_browser_fallback
fi

log "agent_gui complete (IG_AGENT_API_URL=${API_URL})"
exit 0
