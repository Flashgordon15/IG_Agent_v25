#!/usr/bin/env bash
# Trading Desk viewer KeepAlive — UI/shell only. NEVER starts, stops, or signals agents.
# Used by LaunchAgent com.igagent.trading_desk (KeepAlive). Safe while A2 CFD pause is on.
set -euo pipefail

ROOT="/Users/chrisgordon/Projects/IG_Agent_v25"
cd "$ROOT"

# If interim offline hold is set, park without launching UI (avoids KeepAlive thrash
# should the LaunchAgent be re-enabled accidentally).
if [[ -f "${ROOT}/src/data/v31-production/state/desk_offline_hold.json" ]] \
  || [[ -f "${ROOT}/src/data/state/desk_offline_hold.json" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] desk_offline_hold active — viewer keepalive parked" \
    >>"${ROOT}/logs/trading_desk_viewer_keepalive.log" 2>/dev/null || true
  exec sleep infinity
fi

export PATH="${ROOT}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="${ROOT}/src"
export VIRTUAL_ENV="${ROOT}/.venv"
export IG_AGENT_ROOT="${ROOT}"
export IG_AGENT_FROM_LAUNCHER=1
export IG_AGENT_MODE=DEMO
export APP_MODE=DEMO
export IG_TRADING_DESK_NATIVE=1
export IG_TERMINAL_UI_PORT=3000
export IG_COCKPIT_URL="http://localhost:3000/boot"
export IG_TRADING_DESK_URL="http://localhost:3000/boot"
export PYTHONDONTWRITEBYTECODE=1
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# Never route through Apex / cold-start trading_desk_silent agent path
unset IG_APEX_DESKTOP IG_AGENT_DESKTOP_LAUNCH IG_AGENT_SHADOW_DESK \
  IG_APEX_LIVE_ONLY NODE_ENV ELECTRON_RUN_AS_NODE IG_APEX_DAEMON 2>/dev/null || true

PY="${ROOT}/.venv/bin/python3"
LOG_DIR="${ROOT}/logs"
LOG_FILE="${LOG_DIR}/trading_desk_viewer_keepalive.log"
TERMINAL_UI_PORT=3000
BOOT_URL="http://localhost:${TERMINAL_UI_PORT}/boot"
DESK_URL="http://127.0.0.1:${TERMINAL_UI_PORT}/desk"
TERMINAL_URL="http://127.0.0.1:${TERMINAL_UI_PORT}/"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

_shell_pids() {
  pgrep -f "cockpit.desktop_app_shell.*3000" 2>/dev/null || true
}

_terminal_ui_ready() {
  curl -sf --max-time 2 "${DESK_URL}" >/dev/null 2>&1 \
    || curl -sf --max-time 2 "${TERMINAL_URL}boot" >/dev/null 2>&1 \
    || curl -sf --max-time 2 "${TERMINAL_URL}" >/dev/null 2>&1
}

_ensure_terminal_ui() {
  if _terminal_ui_ready; then
    log "Quantum Terminal already live on :${TERMINAL_UI_PORT}"
    return 0
  fi
  log "Starting Quantum Terminal via ui_terminal_daemon (viewer-only)"
  # ui_terminal_daemon exits 0 if already listening; otherwise starts UI without touching agents.
  bash "${ROOT}/scripts/ui_terminal_daemon.sh" >>"$LOG_FILE" 2>&1 || true
  for _ in $(seq 1 45); do
    if _terminal_ui_ready; then
      log "Quantum Terminal ready on :${TERMINAL_UI_PORT}"
      return 0
    fi
    sleep 1
  done
  log "ERROR: Quantum Terminal did not become ready on :${TERMINAL_UI_PORT}"
  return 1
}

if [[ ! -x "$PY" ]]; then
  log "ERROR: missing venv python at ${PY}"
  exit 1
fi

log "=== Trading Desk viewer KeepAlive start ==="

# If a shell is already open (e.g. Desktop app), wait on it so launchd owns the session
# without spawning a second window. When it exits, launchd relaunches this script.
EXISTING="$(_shell_pids | head -1 || true)"
if [[ -n "${EXISTING}" ]]; then
  log "Existing desktop_app_shell PID ${EXISTING} — waiting (no agent touch)"
  # Bring to front best-effort
  osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is ${EXISTING}) to true" >/dev/null 2>&1 || true
  while kill -0 "${EXISTING}" 2>/dev/null; do
    sleep 5
  done
  log "Shell PID ${EXISTING} exited — handing back to launchd KeepAlive"
  exit 0
fi

if ! _ensure_terminal_ui; then
  exit 1
fi

# Prefer live CFD PID for shell metadata only; never kill/restart it.
LIVE_AGENT_PID="$(lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null | head -1 || true)"
export IG_TRADING_AGENT_PID="${LIVE_AGENT_PID:-}"

log "Opening Trading Desk shell at ${BOOT_URL} (viewer-only; agent PID meta=${IG_TRADING_AGENT_PID:-none})"
exec "$PY" -m cockpit.desktop_app_shell --cockpit-url "${BOOT_URL}" >> "$LOG_FILE" 2>&1
