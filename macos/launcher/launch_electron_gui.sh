#!/bin/bash
# Launch IG Agent Apex Electron shell (main.js) after agent_start completes.
# Detached — trading agent on :8080 keeps running independently.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"

LOG="${IG_AGENT_ROOT}/logs/electron_gui.log"
PID_FILE="${IG_AGENT_ROOT}/logs/.electron_gui.pid"
MAIN_JS="${IG_AGENT_ROOT}/main.js"

mkdir -p "${IG_AGENT_ROOT}/logs"

if [[ "${LAUNCHER_SKIP_ELECTRON_GUI:-}" == "1" ]]; then
  log "[ELECTRON] skipped (LAUNCHER_SKIP_ELECTRON_GUI=1)"
  exit 0
fi

if [[ ! -f "${MAIN_JS}" ]]; then
  log "[ELECTRON] main.js missing — skip GUI launch"
  exit 0
fi

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
    log "[ELECTRON] already running pid=${old_pid}"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

# Brief health gate — agent must answer before we surface UI.
if ! curl -sf --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null; then
  log "[ELECTRON] WARN: :${PORT}/health not ready — launching GUI anyway"
fi

ELECTRON_DIR="${IG_AGENT_ROOT}/node_modules/electron"
PATH_TXT="${ELECTRON_DIR}/path.txt"
ELECTRON_BIN="${ELECTRON_DIR}/dist/Electron.app/Contents/MacOS/Electron"

# Repair incomplete npm installs (allowScripts blocked postinstall).
if [[ -f "${ELECTRON_DIR}/install.js" ]] && [[ ! -d "${ELECTRON_DIR}/dist/Electron.app/Contents/Frameworks" ]]; then
  log "[ELECTRON] repairing incomplete Electron install"
  if [[ -f "${IG_AGENT_ROOT}/scripts/repair_electron_install.js" ]]; then
    node "${IG_AGENT_ROOT}/scripts/repair_electron_install.js" >> "${LOG}" 2>&1 || true
  else
    (cd "${ELECTRON_DIR}" && node install.js >> "${LOG}" 2>&1) || true
  fi
fi

# Repair missing path.txt (no trailing newline — electron spawn breaks on \n).
if [[ ! -f "${PATH_TXT}" ]] && [[ -x "${ELECTRON_BIN}" ]]; then
  printf '%s' 'Electron.app/Contents/MacOS/Electron' > "${PATH_TXT}"
  log "[ELECTRON] wrote missing path.txt for local Electron binary"
fi

resolve_electron_cmd() {
  if [[ -x "${IG_AGENT_ROOT}/node_modules/.bin/electron" ]]; then
    echo "${IG_AGENT_ROOT}/node_modules/.bin/electron"
    return 0
  fi
  if [[ -x "${ELECTRON_BIN}" ]]; then
    echo "${ELECTRON_BIN}"
    return 0
  fi
  if command -v npx >/dev/null 2>&1; then
    echo "npx electron"
    return 0
  fi
  return 1
}

if ! ELECTRON_CMD="$(resolve_electron_cmd)"; then
  log "[ELECTRON] binary not found — falling back to Iron Cage desktop shell"
  if [[ -x "${SCRIPT_DIR}/agent_gui.sh" ]]; then
    nohup /bin/bash "${SCRIPT_DIR}/agent_gui.sh" >> "${LOG}" 2>&1 &
    echo $! > "${PID_FILE}"
    log "[ELECTRON] fallback agent_gui pid=$(cat "${PID_FILE}")"
    exit 0
  fi
  log "[ELECTRON] ERROR: no Electron binary and no agent_gui.sh fallback"
  exit 1
fi

export IG_APEX_DESKTOP="${IG_APEX_DESKTOP:-1}"
export IG_AGENT_DESKTOP_LAUNCH=1
export IG_APEX_LIVE_ONLY=1
export NODE_ENV="${NODE_ENV:-production}"

log "[ELECTRON] launching Apex shell (${ELECTRON_CMD} .) — log=${LOG}"
(
  cd "${IG_AGENT_ROOT}"
  # shellcheck disable=SC2086
  nohup ${ELECTRON_CMD} . >> "${LOG}" 2>&1 &
  echo $! > "${PID_FILE}"
)
sleep 1
launcher_pid="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
if [[ -n "${launcher_pid}" ]] && kill -0 "${launcher_pid}" 2>/dev/null; then
  log "[ELECTRON] GUI launcher pid=${launcher_pid}"
  launcher_status_set "ready" "Launch complete" "Apex Electron shell starting" 9 9 "" "green"
else
  log "[ELECTRON] WARN: GUI process may have exited — see ${LOG}"
fi
