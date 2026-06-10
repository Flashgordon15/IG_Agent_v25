#!/bin/bash
# IG Agent v29.0 — macOS bundle launcher (Contents/Resources/launch.sh)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_VERSION_LABEL="v29.0"
CONFIG_TIER="config_v29.json"

find_project_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "${dir}/src/main.py" ]; then
      printf '%s\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

LOG_DIR=""
LOG_FILE=""
log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}"
}

notify_failure() {
  local msg="$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"IG Agent ${APP_VERSION_LABEL}\" message \"${msg}\" as warning" 2>/dev/null || true
  fi
}

DASHBOARD_URL="http://localhost:8080/"
HEALTH_URL="http://localhost:8080/health"
API_HEALTH_URL="http://localhost:8080/api/health"

open_dashboard() {
  if command -v open >/dev/null 2>&1; then
    # Use localhost (not 127.0.0.1) so dashboard API_BASE matches CORS allow_origins.
    open -g "${DASHBOARD_URL}" 2>/dev/null || open "${DASHBOARD_URL}" 2>/dev/null || true
  fi
}

dashboard_healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 1 "${HEALTH_URL}" >/dev/null 2>&1
    return $?
  fi
  return 1
}

ensure_watchdog() {
  local wd="${ROOT}/scripts/watchdog.sh"
  if [ ! -x "${wd}" ]; then
    log "WARN: watchdog script missing or not executable (${wd})"
    return 0
  fi
  if command -v launchctl >/dev/null 2>&1; then
    if launchctl print "gui/$(id -u)/com.igagent.v25.watchdog" >/dev/null 2>&1; then
      log "launchd watchdog active — not spawning duplicate"
      return 0
    fi
  fi
  if pgrep -f "${wd}" >/dev/null 2>&1; then
    log "watchdog already running"
    return 0
  fi
  nohup bash "${wd}" >>"${LOG_DIR}/watchdog.log" 2>&1 &
  local wd_pid=$!
  disown "${wd_pid}" 2>/dev/null || true
  log "watchdog started pid=${wd_pid}"
}

lock_holder_alive() {
  local lock_pid=""
  if [ ! -f "${LOCK_FILE}" ]; then
    return 1
  fi
  lock_pid=$(head -1 "${LOCK_FILE}" 2>/dev/null | awk '{print $1}' || true)
  if [ -n "${lock_pid}" ] && kill -0 "${lock_pid}" 2>/dev/null; then
    return 0
  fi
  return 1
}

trading_healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 2 "${API_HEALTH_URL}" 2>/dev/null \
      | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('trading_healthy') else 1)" 2>/dev/null
    return $?
  fi
  return 1
}

wait_for_dashboard() {
  local mode="$1"
  # Startup can take up to 3 minutes: Yahoo OHLC seeding + 22s REST stagger per market
  # + self-test suite. Poll every 0.5s for up to 360s (720 attempts).
  for _ in $(seq 1 720); do
    if dashboard_healthy; then
      if trading_healthy; then
        open_dashboard
        log "dashboard ready — trading healthy (${mode})"
        exit 0
      fi
      log "dashboard up — awaiting trading_healthy (${mode})"
    fi
    sleep 0.5
  done
  if dashboard_healthy; then
    open_dashboard
    log "WARN: dashboard up but trading_healthy not confirmed within 360s (${mode})"
    notify_failure "Agent started but trading is not healthy. Check dashboard and engine.log."
    exit 0
  fi
  log "WARN: dashboard did not become healthy within 360s (${mode})"
  notify_failure "IG Agent did not start. Check src/data/logs/launcher.log"
  exit 1
}

if ! ROOT="$(find_project_root "$SCRIPT_DIR")"; then
  ROOT=""
fi

if [ -z "$ROOT" ] || [ ! -f "${ROOT}/src/main.py" ]; then
  notify_failure "Could not find IG Agent ${APP_VERSION_LABEL} (src/main.py). Reinstall from IG_Agent_v25."
  exit 1
fi

LOG_DIR="${ROOT}/src/data/logs"
LOG_FILE="${LOG_DIR}/launcher.log"
mkdir -p "${LOG_DIR}"
LOCK_FILE="${ROOT}/src/data/.ig_agent_v25.lock"
LEGACY_LOCK_FILE="${ROOT}/src/data/.ig_agent_v24.lock"

log "=== IG Agent ${APP_VERSION_LABEL} launch ==="
log "script_dir=${SCRIPT_DIR}"
log "project_root=${ROOT}"
log "config_tier=${CONFIG_TIER}"

if [ "${LAUNCHER_VALIDATE_ONLY:-}" = "1" ]; then
  printf '%s\n' "${ROOT}"
  exit 0
fi

if [ -f "${ROOT}/emergency_stop.lock" ]; then
  log "ERROR: emergency_stop.lock present"
  notify_failure "Emergency stop lock is set. Delete emergency_stop.lock in the project folder, then retry."
  exit 1
fi

clear_manual_stop_flag() {
  local flag="${ROOT}/src/data/state/manual_stop.json"
  if [ ! -f "${flag}" ]; then
    return 0
  fi
  if IG_AGENT_ROOT="${ROOT}" PYTHONPATH="${ROOT}/src" python3 -c "
from system.shutdown_cleanup import clear_manual_stop
clear_manual_stop()
" 2>/dev/null; then
    log "cleared manual_stop flag — explicit launcher start"
  else
    rm -f "${flag}" && log "cleared manual_stop flag (fallback rm)" || true
  fi
}

code_newer_than_agent() {
  if [ -z "${ROOT}" ]; then
    return 1
  fi
  python3 - "${ROOT}" <<'PY' 2>/dev/null
import subprocess
import sys
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1])
markers = [
    root / "src" / "main.py",
    root / "src" / "api" / "routes.py",
    root / "dashboard" / "dist" / "index.html",
    root / "config" / "config_v29.json",
]
try:
    pid = subprocess.check_output(
        ["lsof", "-t", "-iTCP:8080", "-sTCP:LISTEN"],
        text=True,
    ).strip().splitlines()[0]
except (subprocess.CalledProcessError, IndexError):
    raise SystemExit(1)
try:
    started = subprocess.check_output(["ps", "-p", pid, "-o", "lstart="], text=True).strip()
    start_epoch = datetime.strptime(started, "%a %b %d %H:%M:%S %Y").timestamp()
except (subprocess.CalledProcessError, ValueError):
    raise SystemExit(1)
latest = max((p.stat().st_mtime for p in markers if p.is_file()), default=0.0)
raise SystemExit(0 if latest > start_epoch + 1 else 1)
PY
}

notify_user() {
  local msg="$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${msg}\" with title \"IG Agent ${APP_VERSION_LABEL}\"" 2>/dev/null || true
  fi
}

if dashboard_healthy; then
  ensure_watchdog
  log "agent already running — opening dashboard"
  if code_newer_than_agent; then
    log "WARN: code on disk is newer than running agent — Stop Agent then relaunch to load changes"
    notify_user "Code updated since this session started. Use Stop Agent, then launch again."
  else
    notify_user "Opening dashboard…"
  fi
  open_dashboard
  exit 0
fi

if command -v launchctl >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/com.igagent.v25.watchdog" >/dev/null 2>&1; then
    clear_manual_stop_flag
    log "launchd watchdog active — waiting for watchdog to start agent (no duplicate main.py)"
    notify_user "Launchd starting agent… dashboard will open when ready."
    wait_for_dashboard "launchd cold start"
  fi
fi

clear_manual_stop_flag
notify_user "Starting IG Agent… dashboard will open when ready."
log "startup notification sent — awaiting agent health"

if lock_holder_alive; then
  log "instance lock held — waiting for dashboard health"
  wait_for_dashboard "existing instance"
fi

if ! cd "${ROOT}"; then
  log "ERROR: cannot cd to project root"
  notify_failure "Cannot open project folder."
  exit 1
fi

export IG_AGENT_ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

clear_stale_lock_file() {
  local file="$1"
  local lock_pid=""
  if [ ! -f "${file}" ]; then
    return 0
  fi
  lock_pid=$(head -1 "${file}" 2>/dev/null | awk '{print $1}' || true)
  if [ -z "${lock_pid}" ]; then
    rm -f "${file}" && log "removed empty instance lock ${file}" || true
    return 0
  fi
  if kill -0 "${lock_pid}" 2>/dev/null; then
    return 0
  fi
  rm -f "${file}" && log "removed stale instance lock ${file} (pid=${lock_pid} not running)" || true
}

clear_stale_lock() {
  clear_stale_lock_file "${LEGACY_LOCK_FILE}"
  clear_stale_lock_file "${LOCK_FILE}"
}

clear_stale_lock

PY=""
for candidate in \
  "${ROOT}/.venv/bin/python3" \
  "${ROOT}/venv/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
  "/opt/homebrew/bin/python3" \
  "/usr/local/bin/python3" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
    PY="${candidate}"
    break
  fi
done

if [ -z "${PY}" ]; then
  log "ERROR: no python3 executable found"
  notify_failure "Python 3 not found. Install Python 3.14 and retry."
  exit 1
fi

log "python=${PY}"

ENTRY="${ROOT}/src/main.py"
if [ ! -f "${ENTRY}" ]; then
  log "ERROR: missing ${ENTRY}"
  notify_failure "main.py not found."
  exit 1
fi

CAFF_ARGS=()
if command -v caffeinate >/dev/null 2>&1; then
  CAFF_ARGS=(caffeinate -i -s)
  log "caffeinate enabled (-i -s) — prevents sleep while agent runs"
else
  log "WARN: caffeinate not found — Mac may sleep and stop the agent overnight"
fi

log "launch: cd ${ROOT} && PYTHONPATH=src ${CAFF_ARGS[*]:-} ${PY} src/main.py"

WATCHDOG_SCRIPT="${ROOT}/scripts/watchdog.sh"

# Start (or restart) the watchdog — skip if launchd owns supervision.
if command -v launchctl >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/com.igagent.v25.watchdog" >/dev/null 2>&1; then
    log "launchd watchdog active — skipping manual watchdog spawn"
  elif [ -x "${WATCHDOG_SCRIPT}" ]; then
    bash "${WATCHDOG_SCRIPT}" >> "${ROOT}/src/data/logs/watchdog.log" 2>&1 &
    WATCHDOG_PID=$!
    disown "${WATCHDOG_PID}" 2>/dev/null || true
    log "watchdog started pid=${WATCHDOG_PID}"
  else
    log "WARN: watchdog not found or not executable at ${WATCHDOG_SCRIPT}"
  fi
else
  pkill -f "watchdog.sh" 2>/dev/null || true
  sleep 0.5
  WATCHDOG_SCRIPT="${ROOT}/scripts/watchdog.sh"
  if [ -x "${WATCHDOG_SCRIPT}" ]; then
      bash "${WATCHDOG_SCRIPT}" >> "${ROOT}/src/data/logs/watchdog.log" 2>&1 &
      WATCHDOG_PID=$!
      disown "${WATCHDOG_PID}" 2>/dev/null || true
      log "watchdog started pid=${WATCHDOG_PID}"
  else
      log "WARN: watchdog not found or not executable at ${WATCHDOG_SCRIPT}"
  fi
fi

# Launcher opens the dashboard once health is up; tell main.py not to open again.
export IG_AGENT_FROM_LAUNCHER=1
(
  cd "${ROOT}" || exit 1
  export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
  if ((${#CAFF_ARGS[@]})); then
    exec "${CAFF_ARGS[@]}" "${PY}" src/main.py
  else
    exec "${PY}" src/main.py
  fi
) >>"${LOG_FILE}" 2>&1 &
CHILD=$!
disown "${CHILD}" 2>/dev/null || true
log "started pid=${CHILD} (background, no terminal)"

wait_for_dashboard "new instance"
