#!/usr/bin/env bash
# Start IG Agent v25 in background (caffeinate + main.py).
# Used by watchdog restarts and mirrors the desktop launcher agent start.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${AGENT_DIR}/src/data/logs"
mkdir -p "${LOG_DIR}"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_DIR}/agent_restart.log"
}

PY=""
for candidate in \
  "${AGENT_DIR}/.venv/bin/python3" \
  "${AGENT_DIR}/venv/bin/python3" \
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
  exit 1
fi

CAFF_ARGS=()
if command -v caffeinate >/dev/null 2>&1; then
  CAFF_ARGS=(caffeinate -i -s)
fi

export IG_AGENT_ROOT="${AGENT_DIR}"
export IG_AGENT_FROM_LAUNCHER=1
export PYTHONPATH="${AGENT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

log "start_agent_background: python=${PY} caffeinate=${CAFF_ARGS[*]:-off}"

cd "${AGENT_DIR}"
if ((${#CAFF_ARGS[@]})); then
  exec "${CAFF_ARGS[@]}" "${PY}" src/main.py
else
  exec "${PY}" src/main.py
fi
