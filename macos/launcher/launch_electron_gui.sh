#!/bin/bash
# Launch native Trading Desk shell (pywebview) — Apex Electron permanently disabled.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"

LOG="${IG_AGENT_ROOT}/logs/trading_desk_gui.log"
TERMINAL_UI_PORT="${IG_TERMINAL_UI_PORT:-3000}"
DESK_URL="http://localhost:${TERMINAL_UI_PORT}/desk"

mkdir -p "${IG_AGENT_ROOT}/logs"

if [[ "${LAUNCHER_SKIP_ELECTRON_GUI:-}" == "1" ]]; then
  log "[DESK] skipped (LAUNCHER_SKIP_ELECTRON_GUI=1)"
  exit 0
fi

# Kill any stale Apex Electron shells from prior sessions.
pkill -f "${IG_AGENT_ROOT}/node_modules/electron/dist/Electron.app" 2>/dev/null || true

if ! curl -sf --max-time 3 "http://127.0.0.1:${IG_API_PORT:-8080}/api/health" >/dev/null; then
  log "[DESK] WARN: :${IG_API_PORT:-8080}/api/health not ready — launching shell anyway"
fi
if ! curl -sf --max-time 3 "http://127.0.0.1:8081/api/health" >/dev/null; then
  log "[DESK] WARN: :8081/api/health not ready — SB lane may be down"
fi
if ! curl -sf --max-time 3 "${DESK_URL}" >/dev/null; then
  log "[DESK] WARN: Quantum Terminal ${DESK_URL} not ready — ensure start_ui_background.sh"
fi

export IG_TRADING_DESK_NATIVE=1
export IG_TERMINAL_UI_PORT="${TERMINAL_UI_PORT}"
export IG_COCKPIT_URL="${DESK_URL}"
export IG_TRADING_DESK_URL="${DESK_URL}"
unset IG_APEX_DESKTOP IG_AGENT_DESKTOP_LAUNCH IG_AGENT_SHADOW_DESK 2>/dev/null || true

log "[DESK] launching native Trading Desk shell (pywebview) at ${DESK_URL}"
# shellcheck source=../../scripts/lib/detach_exec.sh
source "${IG_AGENT_ROOT}/scripts/lib/detach_exec.sh"
detach_exec --log "${LOG}" -- \
  "${IG_AGENT_PY}" -m cockpit.desktop_app_shell --cockpit-url "${DESK_URL}"
echo "${DETACH_PID}" > "${IG_AGENT_ROOT}/logs/.trading_desk_desktop.pid"
launcher_status_set "ready" "Launch complete" "Trading Desk native shell" 9 9 "" "green"
