#!/usr/bin/env bash
# IG Trading Agent v34 — Native Desktop Launcher (pywebview shell)
# Multiplex desk: Quantum Terminal :3000/desk + dual-port engines :8080/:8081.

set -euo pipefail

ROOT="/Users/chrisgordon/Projects/IG_Agent_v25"
cd "$ROOT"

export PYTHONPATH="${ROOT}/src"
export PATH="${ROOT}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export VIRTUAL_ENV="${ROOT}/.venv"
export IG_AGENT_ROOT="${ROOT}"
export IG_AGENT_FROM_LAUNCHER=1
export IG_AGENT_MODE=DEMO
export APP_MODE=DEMO
export IG_API_PORT=8080
export IG_NODE_PROFILE=production
export PROD_MODE=PRODUCTION
export IG_SHARE_ENGINE=1
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export IG_TRADING_DESK_NATIVE=1
export IG_TERMINAL_UI_PORT=3000
export IG_COCKPIT_URL="http://localhost:3000/boot"
export IG_TRADING_DESK_URL="http://localhost:3000/boot"
export PYTHONDONTWRITEBYTECODE=1
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# Never route through Apex shadow desktop profile
unset IG_APEX_DESKTOP IG_AGENT_DESKTOP_LAUNCH IG_AGENT_SHADOW_DESK \
  IG_APEX_LIVE_ONLY NODE_ENV ELECTRON_RUN_AS_NODE IG_APEX_DAEMON 2>/dev/null || true

LAUNCH_ENV="${ROOT}/config/credentials/launch.env"
if [[ -f "${LAUNCH_ENV}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${LAUNCH_ENV}"
  set +a
fi
if [[ -n "${AGENT_LAUNCH_PASS:-}" ]]; then
  export AGENT_LAUNCH_PASS
  export ADMIN_PASSWORD="${ADMIN_PASSWORD:-${AGENT_LAUNCH_PASS}}"
fi

PY="${ROOT}/.venv/bin/python3"
LOG_DIR="${ROOT}/logs"
LOG_FILE="${LOG_DIR}/production_runtime.log"
CFD_PORT=8080
SB_PORT=8081
TERMINAL_UI_PORT=3000
TERMINAL_URL="http://127.0.0.1:${TERMINAL_UI_PORT}/"
BOOT_URL="http://localhost:${TERMINAL_UI_PORT}/boot"
DESK_URL="http://127.0.0.1:${TERMINAL_UI_PORT}/desk"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"; }

_port_listening() {
  local port="$1"
  lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
}

_engine_health_ok() {
  local port="$1"
  curl -sf --max-time 2 "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1
}

_port_breathing() {
  local port="$1"
  if _port_listening "${port}"; then
    return 0
  fi
  _engine_health_ok "${port}"
}

_dual_engines_breathing() {
  _port_breathing "${CFD_PORT}" && _port_breathing "${SB_PORT}"
}

_live_engine_pid() {
  local port="$1"
  local pid=""
  pid="$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  if [[ -n "${pid}" ]]; then
    echo "${pid}"
    return 0
  fi
  return 1
}

_health_json() {
  curl -sf "http://127.0.0.1:${CFD_PORT}/api/health" 2>/dev/null || true
}

_agent_healthy_v31() {
  local raw
  raw="$(_health_json)"
  [[ -n "$raw" ]] || return 1
  echo "$raw" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
if not d.get('ok'):
    raise SystemExit(1)
root = str(d.get('data_root') or '')
cfg = str(d.get('config_overlay') or '')
if 'v31' not in root and 'config_v31' not in cfg:
    raise SystemExit(1)
print(d.get('agent_pid') or '')
" 2>/dev/null
}

_terminal_ui_ready() {
  curl -sf --max-time 2 "${DESK_URL}" >/dev/null 2>&1 \
    || curl -sf --max-time 2 "${TERMINAL_URL}boot" >/dev/null 2>&1 \
    || curl -sf --max-time 2 "${TERMINAL_URL}" >/dev/null 2>&1
}

_ensure_terminal_ui() {
  if _terminal_ui_ready; then
    log "  Quantum Terminal already live on :3000"
    return 0
  fi
  log "  Starting Quantum Terminal (Next.js) on :3000..."
  if [[ ! -x "${ROOT}/scripts/start_ui_background.sh" ]]; then
    log "  ERROR: start_ui_background.sh missing"
    return 1
  fi
  bash "${ROOT}/scripts/start_ui_background.sh" >>"$LOG_FILE" 2>&1 || true
  for _ in $(seq 1 45); do
    if _terminal_ui_ready; then
      log "  Quantum Terminal ready on :3000"
      return 0
    fi
    sleep 1
  done
  log "  ERROR: Quantum Terminal did not start on :3000"
  return 1
}

_shell_running() {
  pgrep -f "cockpit.desktop_app_shell.*3000" >/dev/null 2>&1
}

_bring_shell_front() {
  local shell_pid
  shell_pid="$(pgrep -f 'cockpit.desktop_app_shell.*3000' | head -1 || true)"
  if [[ -z "${shell_pid}" ]]; then
    return 1
  fi
  osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is ${shell_pid}) to true" >/dev/null 2>&1 || true
  log "  Brought Trading Desk shell to front (PID ${shell_pid})"
  return 0
}

_open_trading_desk_shell() {
  local agent_pid="${1:-}"
  export IG_TRADING_AGENT_PID="${agent_pid}"
  exec "$PY" -m cockpit.desktop_app_shell --cockpit-url "${BOOT_URL}" >> "$LOG_FILE" 2>&1
}

if [[ ! -x "$PY" ]]; then
  osascript -e 'display alert "IG Trading Agent" message "Python venv not found at .venv" as critical' 2>/dev/null || true
  exit 1
fi

MAX_LOG=$((20 * 1024 * 1024))
if [[ -f "$LOG_FILE" ]] && [[ $(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0) -gt $MAX_LOG ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.prev"
fi

log "=== Trading Desk native launch (v34 multiplex :3000/desk + :8080/:8081) ==="

# Fast path: dual-port engines breathing — never tear down live trading.
LIVE_AGENT_PID=""
if _dual_engines_breathing; then
  LIVE_AGENT_PID="$(_live_engine_pid "${CFD_PORT}" || true)"
  log "  dual-port breathing — CFD :${CFD_PORT} SB :${SB_PORT} (CFD PID ${LIVE_AGENT_PID:-?})"
elif [[ -n "$(_agent_healthy_v31 || true)" ]]; then
  LIVE_AGENT_PID="$(_agent_healthy_v31 || true)"
  log "  single-engine healthy on :${CFD_PORT} (PID ${LIVE_AGENT_PID})"
fi

if [[ -n "${LIVE_AGENT_PID}" ]] || _dual_engines_breathing; then
  if _shell_running; then
    log "Engines live + Trading Desk shell already open — bring front"
    _bring_shell_front || true
    # Ensure :3000 still answers; restart UI only if dead.
    if ! _terminal_ui_ready; then
      log "  :3000 down while shell live — restarting Quantum Terminal"
      _ensure_terminal_ui || true
    fi
    exit 0
  fi
  if ! _ensure_terminal_ui; then
    osascript -e 'display alert "IG Trading Agent" message "Quantum Terminal failed to start on port 3000. Check logs/production_runtime.log" as critical' 2>/dev/null || true
    exit 1
  fi
  log "Engines live — opening Quantum Terminal shell at ${BOOT_URL}"
  _open_trading_desk_shell "${LIVE_AGENT_PID:-}"
fi

# Kill legacy Apex Electron shells
pkill -f "${ROOT}/node_modules/electron/dist/Electron.app" 2>/dev/null || true

# --- Step 1: Anti-zombie shutdown (never kill -9 main) ---
log "[1/5] Anti-zombie daemon cleanse..."

WATCHDOG_PLIST="$HOME/Library/LaunchAgents/com.igagent.v25.watchdog.plist"
AGENT_PLIST="$HOME/Library/LaunchAgents/com.igagent.v25.plist"
CAFF_PLIST="$HOME/Library/LaunchAgents/com.igagent.v25.caffeinate.plist"

for PLIST in "$WATCHDOG_PLIST" "$AGENT_PLIST" "$CAFF_PLIST"; do
  LABEL=$(basename "$PLIST" .plist)
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    launchctl unload "$PLIST" 2>/dev/null && log "  Unloaded $LABEL" || true
  fi
done

"$PY" -c "
from system.shutdown_cleanup import mark_manual_stop
mark_manual_stop(source='trading_desk_desktop')
" 2>/dev/null || true

LIVE_PID=""
for pid_file in \
  "${ROOT}/src/data/v31-production/agent.pid" \
  "${ROOT}/src/data/v31-production/state/agent.pid" \
  "${ROOT}/src/data/v31-production/supervisor.pid"; do
  if [[ -f "${pid_file}" ]]; then
  candidate=$(tr -d '[:space:]' < "${pid_file}" 2>/dev/null || true)
  if [[ -n "${candidate}" ]] && ps -p "${candidate}" >/dev/null 2>&1; then
    LIVE_PID="${candidate}"
    break
  fi
  fi
done
if [[ -z "$LIVE_PID" ]]; then
  LIVE_PID="$(pgrep -f 'src/main.py' | head -1 || true)"
fi

if [[ -n "$LIVE_PID" ]]; then
  log "  SIGTERM to main PID $LIVE_PID (no kill -9)"
  kill -TERM "$LIVE_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do
    if ! ps -p "$LIVE_PID" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if ps -p "$LIVE_PID" >/dev/null 2>&1; then
    log "  ERROR: main PID $LIVE_PID still alive after 30s TERM — aborting (anti-zombie)"
    osascript -e 'display alert "IG Trading Agent" message "Agent did not exit after SIGTERM. Refusing kill -9. Check logs/production_runtime.log" as critical' 2>/dev/null || true
    exit 1
  fi
  log "  Previous agent terminated."
fi

# --- Step 2: Port cleanup (TERM only; wait for 8080 free) ---
log "[2/5] Port cleanup (TERM only)..."

for PORT in 49151 8080 9090; do
  PIDS=$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
  if [[ -n "$PIDS" ]]; then
    for PID in $PIDS; do
      [[ "$PID" != "$$" ]] && kill -TERM "$PID" 2>/dev/null || true
    done
  fi
done

for _ in $(seq 1 30); do
  if ! lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  log "  ERROR: port 8080 still bound after TERM wait — aborting (anti-zombie)"
  osascript -e 'display alert "IG Trading Agent" message "Port 8080 still bound after graceful shutdown. Refusing kill -9." as critical' 2>/dev/null || true
  exit 1
fi

rm -f "${ROOT}/src/data/.ig_agent_v29.lock" \
  "${ROOT}/src/data/.ig_agent_v30_port_8080.lock" \
  "${ROOT}/src/data/.ig_agent_v31_port_8080.lock" 2>/dev/null || true
find "${ROOT}/src" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "${ROOT}/src" -name '*.pyc' -delete 2>/dev/null || true
log "  Ports clear, locks + bytecode evicted."

# --- Step 3: Scheduled ops (keep manual_stop until agent healthy) ---
log "[3/5] Installing scheduled ops (manual_stop held until healthy)..."
if [[ -x "${ROOT}/scripts/install_launchd.sh" ]]; then
  bash "${ROOT}/scripts/install_launchd.sh" --ops-only >> "$LOG_FILE" 2>&1 || true
fi
log "  Scheduled ops installed; manual_stop still active."

# --- Step 4: Start agent ---
log "[4/5] Launching agent..."

"${PY}" -c "
from system.startup_hold_clear import clear_stale_entry_holds_if_flat
r = clear_stale_entry_holds_if_flat(port=${CFD_PORT}, reason='trading_desk_cold_start', allow_offline_stale_clear=True)
print('startup_hold_clear', r)
" 2>/dev/null || true

export IG_AGENT_OPEN_COCKPIT=0
# shellcheck source=lib/detach_exec.sh
source "${ROOT}/scripts/lib/detach_exec.sh"
detach_exec --log "$LOG_FILE" -- "$PY" -u src/main.py
AGENT_PID="${DETACH_PID}"
log "  Agent started as PID $AGENT_PID"

API_OK=0
for i in $(seq 1 45); do
  if curl -sf "http://127.0.0.1:8080/api/health" >/dev/null 2>&1; then
    log "  API healthy at ${i}s"
    API_OK=1
    break
  fi
  sleep 2
done
if [[ "${API_OK}" -ne 1 ]]; then
  log "  ERROR: API not healthy after 90s — leaving manual_stop engaged"
  osascript -e 'display alert "IG Trading Agent" message "Agent failed to start on port 8080. Check logs/production_runtime.log" as critical' 2>/dev/null || true
  exit 1
fi

"$PY" -c "
from system.shutdown_cleanup import clear_manual_stop
clear_manual_stop()
" 2>/dev/null || true
log "  Manual stop cleared"

# Re-arm launchd supervision after --ops-only cold start. ops-only installs
# plists but skips caffeinate/watchdog bootstrap — claiming "re-armed" without
# this step left overnight recovery dark. Prefer bootstrap (not kickstart -k)
# when the agent is already healthy so we do not fight the live PID.
_rearm_watchdog_after_healthy() {
  local domain="gui/$(id -u)"
  local launch_agents="${HOME}/Library/LaunchAgents"
  local label
  # Evict orphan shell watchdog that conflicts with launchd watchdog_launchd.py
  if pgrep -f "${ROOT}/scripts/watchdog.sh" >/dev/null 2>&1; then
    if ! launchctl print "${domain}/com.igagent.v25.watchdog" >/dev/null 2>&1 \
      || [[ "$(launchctl print "${domain}/com.igagent.v25.watchdog" 2>/dev/null | awk '/state =/{print $3; exit}')" != "running" ]]; then
      log "  Stopping orphan scripts/watchdog.sh (conflicts with launchd)"
      pkill -TERM -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true
      sleep 1
    fi
  fi
  for label in \
    com.igagent.v25.caffeinate \
    com.igagent.v25.watchdog \
    com.igagent.v25.profitability; do
    local plist="${launch_agents}/${label}.plist"
    if [[ ! -f "${plist}" ]]; then
      log "  WARN: missing ${plist} — run install_launchd.sh first"
      continue
    fi
    if launchctl print "${domain}/${label}" >/dev/null 2>&1; then
      # Already registered — soft kick without -k when API is healthy
      if [[ "${label}" == "com.igagent.v25.watchdog" ]]; then
        local state
        state="$(launchctl print "${domain}/${label}" 2>/dev/null | awk '/state =/{print $3; exit}' || true)"
        if [[ "${state}" != "running" ]]; then
          launchctl kickstart "${domain}/${label}" 2>/dev/null \
            && log "  Watchdog kickstarted (was ${state:-idle})" \
            || log "  WARN: watchdog kickstart failed"
        else
          log "  Watchdog already running under launchd"
        fi
      fi
      continue
    fi
    if launchctl bootstrap "${domain}" "${plist}" 2>/dev/null; then
      log "  Bootstrapped ${label}"
    else
      log "  WARN: bootstrap failed for ${label}"
    fi
  done
  # Never also load com.igagent.v25.plist — two starters fight.
  launchctl bootout "${domain}/com.igagent.v25" 2>/dev/null || true
  if launchctl print "${domain}/com.igagent.v25.watchdog" >/dev/null 2>&1; then
    log "  Watchdog re-armed under launchd (agent PID ${AGENT_PID} left running)"
  else
    log "  ERROR: watchdog not loaded after re-arm attempt"
  fi
}
_rearm_watchdog_after_healthy


# --- Step 5: Quantum Terminal + native shell ---
log "[5/5] Starting Quantum Terminal + native shell..."
if ! _ensure_terminal_ui; then
  osascript -e 'display alert "IG Trading Agent" message "Quantum Terminal failed to start on port 3000." as critical' 2>/dev/null || true
  exit 1
fi

_open_trading_desk_shell "${AGENT_PID}"
