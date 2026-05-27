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

log "=== IG Agent v25 launch ==="
log "script_dir=${SCRIPT_DIR}"
log "project_root=${ROOT}"

if [ -f "${ROOT}/emergency_stop.lock" ]; then
  log "ERROR: emergency_stop.lock present"
  notify_failure "Emergency stop lock is set. Delete emergency_stop.lock in the project folder, then retry."
  exit 1
fi

if ! cd "${ROOT}"; then
  log "ERROR: cannot cd to project root"
  notify_failure "Cannot open project folder."
  exit 1
fi

export IG_AGENT_ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

LOCK_FILE="${ROOT}/src/data/.ig_agent_v24.lock"

clear_stale_lock() {
  local lock_pid=""
  if [ ! -f "${LOCK_FILE}" ]; then
    return 0
  fi
  lock_pid=$(head -1 "${LOCK_FILE}" 2>/dev/null | awk '{print $1}' || true)
  if [ -z "${lock_pid}" ]; then
    rm -f "${LOCK_FILE}" && log "removed empty instance lock" || true
    return 0
  fi
  if kill -0 "${lock_pid}" 2>/dev/null; then
    return 0
  fi
  rm -f "${LOCK_FILE}" && log "removed stale instance lock (pid=${lock_pid} not running)" || true
}

kill_stale_main_pids() {
  local pattern pid
  for pattern in \
    "${ROOT}/src/main.py" \
    "IG_Agent_v25/src/main.py"
  do
    pids=$(pgrep -f "${pattern}" 2>/dev/null || true)
    for pid in ${pids}; do
      [ -z "${pid}" ] && continue
      [ "${pid}" = "$$" ] && continue
      [ "${pid}" = "${PPID}" ] && continue
      kill -TERM "${pid}" 2>/dev/null && log "terminated stale pid=${pid} (pattern=${pattern})" || true
    done
  done
}

if command -v pgrep >/dev/null 2>&1; then
  clear_stale_lock
  kill_stale_main_pids
  sleep 1
  kill_stale_main_pids
fi

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

if [ "${LAUNCHER_VALIDATE_ONLY:-}" = "1" ]; then
  printf '%s\n' "${ROOT}"
  exit 0
fi

log "exec ${PY} ${ENTRY}"
exec "${PY}" "${ENTRY}" >>"${LOG_FILE}" 2>&1
