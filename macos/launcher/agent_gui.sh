#!/bin/bash
# Launch IG Cockpit — native WKWebView shell only when LAUNCHER_DESKTOP=1.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"

PORT="${IG_API_PORT:-8080}"
# shellcheck source=lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

LOG="${IG_AGENT_ROOT}/logs/cockpit_launch.log"
API_URL="http://127.0.0.1:${PORT}"
PID_FILE="${IG_AGENT_ROOT}/logs/.cockpit.pid"

mkdir -p "${IG_AGENT_ROOT}/logs"
exec >> "${LOG}" 2>&1

log "========== agent_gui cockpit launch port=${PORT} =========="
launcher_status_set "gui" "Stage 9 — Cockpit" "Native Flight Deck shell" 9 9

# Desktop mode: parent WKWebView shell already owns the UI — never spawn browser/Tauri/2nd shell.
if [[ "${LAUNCHER_DESKTOP:-}" == "1" ]] || [[ "${IG_DESKTOP_SHELL_ACTIVE:-}" == "1" ]] || [[ "${IG_DESKTOP_FLIGHT_DECK:-}" == "1" ]]; then
  log "agent_gui: native shell active — deprecating browser/Tauri/legacy splash paths"
  launcher_status_set "ready" "Launch complete" "Iron Cage WKWebView (exclusive)" 9 9 "" "green"
  exit 0
fi

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

export IG_AGENT_API_URL="${API_URL}"
export VITE_IG_AGENT_API_URL="${API_URL}"

DESKTOP_LAUNCHER="${SCRIPT_DIR}/desktop_flight_deck.sh"
if [[ -x "${DESKTOP_LAUNCHER}" ]]; then
  log "Launching Iron Cage native desktop shell (exclusive route)"
  export IG_DESKTOP_FLIGHT_DECK=1
  export IG_DESKTOP_SHELL_ACTIVE=1
  nohup /bin/bash "${DESKTOP_LAUNCHER}" >> "${LOG}" 2>&1 &
  echo $! > "${PID_FILE}"
  launcher_status_set "ready" "Launch complete" "Iron Cage desktop shell active" 9 9 "" "green"
  exit 0
fi

log "ERROR: desktop_flight_deck.sh missing — browser/Tauri fallback disabled in hardened build"
launcher_status_fail "GUI failed" "Native shell launcher missing" 9
exit 1
