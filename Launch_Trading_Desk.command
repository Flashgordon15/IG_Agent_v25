#!/usr/bin/env bash
# IG Trading Desk v34 — double-click launcher (multiplex :3000/desk + dual-port :8080/:8081)
#
# Canonical product path: Trading_Desk.app → scripts/trading_desk_silent.sh
# This alias remains for Dock/Spotlight legacy paths.

set -euo pipefail

ROOT="/Users/chrisgordon/Projects/IG_Agent_v25"
SILENT="${ROOT}/scripts/trading_desk_silent.sh"
CFD_PORT=8080
SB_PORT=8081
UI_PORT=3000

_port_breathing() {
  local port="$1"
  if lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  curl -sf --max-time 2 "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1
}

_ui_ready() {
  curl -sf --max-time 2 "http://127.0.0.1:${UI_PORT}/desk" >/dev/null 2>&1 \
    || curl -sf --max-time 2 "http://127.0.0.1:${UI_PORT}/" >/dev/null 2>&1
}

echo "[Trading Desk] port audit — CFD :${CFD_PORT}=$(_port_breathing "${CFD_PORT}" && echo up || echo down) SB :${SB_PORT}=$(_port_breathing "${SB_PORT}" && echo up || echo down) UI :${UI_PORT}=$(_ui_ready && echo up || echo down)"

if [[ ! -x "${SILENT}" ]]; then
  osascript -e 'display alert "IG Trading Desk" message "scripts/trading_desk_silent.sh missing or not executable" as critical' 2>/dev/null || true
  echo "ERROR: ${SILENT} missing" >&2
  exit 1
fi

exec bash "${SILENT}"
