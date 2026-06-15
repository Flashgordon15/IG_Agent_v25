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
    # Fresh URL forces full SPA reload so the 3-stage launch sequence always runs.
    # -g opens in background so Finder does not try to re-activate this .app.
    local launch_url="${DASHBOARD_URL}?launch=$(date +%s)"
    open -g "${launch_url}" 2>/dev/null || open "${launch_url}" 2>/dev/null || true
  fi
}

dashboard_healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 1 "${HEALTH_URL}" >/dev/null 2>&1
    return $?
  fi
  return 1
}

dashboard_ui_ready() {
  if ! command -v curl >/dev/null 2>&1; then
    return 1
  fi
  local body=""
  # -f treats HTTP 404 (FastAPI JSON) as failure — only HTML 200 passes.
  body="$(curl -sf --max-time 2 "${DASHBOARD_URL}" 2>/dev/null)" || return 1
  if [[ "$body" == *'"detail"'* ]] || [[ "$body" == *'"Not Found"'* ]]; then
    return 1
  fi
  if [[ "$body" == *"<!DOCTYPE"* ]] || [[ "$body" == *"<html"* ]]; then
    return 0
  fi
  return 1
}

force_restart_stale_agent() {
  log "forcing agent restart — dashboard UI or code refresh required"
  if command -v lsof >/dev/null 2>&1; then
    for pid in $(lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null); do
      kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in $(lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null); do
      kill -KILL "$pid" 2>/dev/null || true
    done
  fi
  if command -v pgrep >/dev/null 2>&1; then
    for pid in $(pgrep -f "${ROOT}/src/main.py" 2>/dev/null); do
      kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in $(pgrep -f "${ROOT}/src/main.py" 2>/dev/null); do
      kill -KILL "$pid" 2>/dev/null || true
    done
  fi
  rm -f "${LOCK_FILE}" "${LEGACY_LOCK_FILE}"
  clear_manual_stop_flag
  sleep 1
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
    curl -sf --max-time 2 -H "User-Agent: IG-Agent-Watchdog/1" "${API_HEALTH_URL}" 2>/dev/null \
      | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('trading_healthy') else 1)" 2>/dev/null
    return $?
  fi
  return 1
}

wait_for_dashboard() {
  local mode="$1"
  local opened="0"
  # Wait up to 12 minutes for :8080 + dashboard HTML (OHLC bootstrap on cold start).
  for _ in $(seq 1 1440); do
    if dashboard_healthy && dashboard_ui_ready; then
      if [ "$opened" = "0" ]; then
        open_dashboard
        opened=1
        log "dashboard UI open (${mode})"
      fi
      # Success once the SPA is reachable — trading may still be warming up in the boot bar.
      exit 0
    fi
    sleep 0.5
  done
  log "WARN: dashboard did not become reachable within 720s (${mode})"
  if [ -f "${ROOT}/src/data/logs/engine.log" ]; then
    notify_failure "Agent did not reach healthy state in 12 minutes. Check src/data/logs/engine.log and watchdog.log — OHLC bootstrap may still be running."
  else
    notify_failure "IG Agent did not start. Check src/data/logs/launcher.log"
  fi
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
LOCK_FILE="${ROOT}/src/data/.ig_agent_v29.lock"
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
    root / "src" / "api" / "server.py",
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
  if ! dashboard_ui_ready || code_newer_than_agent; then
    if code_newer_than_agent; then
      log "code on disk is newer than running agent — restarting for dashboard UI"
      notify_user "Updating agent to latest code…"
    else
      log "WARN: :8080 health OK but / missing dashboard HTML — restarting agent"
      notify_user "Restarting agent for dashboard UI…"
    fi
    force_restart_stale_agent
  else
    ensure_watchdog
    log "agent already running with dashboard UI — opening browser"
    notify_user "Opening dashboard…"
    open_dashboard
    exit 0
  fi
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

# Watchdog is started by main.py (_ensure_watchdog_running) after the API port binds —
# spawning here races bootstrap and causes duplicate restarts during OHLC load.

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
