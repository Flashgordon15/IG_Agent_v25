#!/bin/bash
# IG Agent v25 — macOS bundle launcher (Contents/Resources/launch.sh)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
    osascript -e "display alert \"IG Agent v25\" message \"${msg}\" as warning" 2>/dev/null || true
  fi
}

DASHBOARD_URL="http://localhost:8080/"
HEALTH_URL="http://localhost:8080/health"

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

wait_for_dashboard() {
  local mode="$1"
  # Cold start can take ~30–40s (preflight + trading/stream hooks before /health).
  for _ in $(seq 1 240); do
    if dashboard_healthy; then
      open_dashboard
      log "dashboard ready (${mode})"
      exit 0
    fi
    sleep 0.25
  done
  log "WARN: dashboard did not become healthy within 60s (${mode})"
  notify_failure "IG Agent did not start. Check src/data/logs/launcher.log"
  exit 1
}

if ! ROOT="$(find_project_root "$SCRIPT_DIR")"; then
  ROOT=""
fi

if [ -z "$ROOT" ] || [ ! -f "${ROOT}/src/main.py" ]; then
  notify_failure "Could not find IG Agent v25 (src/main.py). Reinstall from IG_Agent_v25."
  exit 1
fi

LOG_DIR="${ROOT}/src/data/logs"
LOG_FILE="${LOG_DIR}/launcher.log"
mkdir -p "${LOG_DIR}"
LOCK_FILE="${ROOT}/src/data/.ig_agent_v25.lock"
LEGACY_LOCK_FILE="${ROOT}/src/data/.ig_agent_v24.lock"

log "=== IG Agent v25 launch ==="
log "script_dir=${SCRIPT_DIR}"
log "project_root=${ROOT}"

if [ "${LAUNCHER_VALIDATE_ONLY:-}" = "1" ]; then
  printf '%s\n' "${ROOT}"
  exit 0
fi

if [ -f "${ROOT}/emergency_stop.lock" ]; then
  log "ERROR: emergency_stop.lock present"
  notify_failure "Emergency stop lock is set. Delete emergency_stop.lock in the project folder, then retry."
  exit 1
fi

if dashboard_healthy; then
  log "agent already running — opening dashboard"
  open_dashboard
  exit 0
fi

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
