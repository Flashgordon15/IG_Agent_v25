#!/usr/bin/env bash
# Apex Monolith — purge shadow desktop ports ONLY (production :8080/:8787 protected).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG="${AGENT_DIR}/src/data/logs/apex_desktop.log"
mkdir -p "$(dirname "${LOG}")"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG}"
}

PROTECT="${IG_APEX_PROTECT_PRODUCTION_PORTS:-1}"
if [[ "${PROTECT}" == "1" ]]; then
  PORTS=(9090 9191)
  log "apex-purge-ports: shadow-only purge (:9090 :9191) — production ports protected"
else
  PORTS=(8080 8787 9090 9191)
  log "apex-purge-ports: full purge (production protection disabled)"
fi

for port in "${PORTS[@]}"; do
  pids="$(lsof -t -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
    log "apex-purge-ports: killed PID(s) on :${port} -> ${pids}"
  fi
done

rm -f "${AGENT_DIR}/src/data/.ig_agent_v30_shadow.lock" 2>/dev/null || true
rm -f "${AGENT_DIR}/src/data/apex_ipc.sock" "${AGENT_DIR}/src/data/apex_ipc_shadow.sock" 2>/dev/null || true

log "apex-purge-ports: complete"
