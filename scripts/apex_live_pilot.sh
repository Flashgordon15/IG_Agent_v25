#!/usr/bin/env bash
# v30 Apex — autonomous live real-world execution & verification pilot.
# Shadow track only — production :8080 is never killed or restarted.
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ISOLATED_ROOT="${HOME}/Library/Application Support/IG Agent Apex/v30-production"
APP_BUNDLE="${AGENT_DIR}/dist-apex/mac-arm64/IG Agent Apex.app"
PILOT_LOG="/tmp/apex_live_pilot_$(date +%Y%m%d_%H%M%S).log"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
REPAIR_LOG="/tmp/apex_pilot_repairs.jsonl"

mkdir -p "${ISOLATED_ROOT}/data/logs" "${ISOLATED_ROOT}/data/state" "${ISOLATED_ROOT}/analytics"
: >"${REPAIR_LOG}"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "${PILOT_LOG}"; }

production_health() {
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8080/api/health" 2>/dev/null) || code="000"
  echo "${code}"
}

shadow_health() {
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:9090/api/health" 2>/dev/null) || code="000"
  echo "${code}"
}

start_shadow_sidecar_dev() {
  log "fallback: start dev-tree shadow sidecar on :9090 (Electron handshake timeout)"
  export NODE_ENV=shadow
  export IG_NODE_PROFILE=shadow
  export IG_APEX_DESKTOP=1
  export IG_APEX_NO_BROWSER=1
  export IG_APEX_PROTECT_PRODUCTION_PORTS=1
  export IG_AGENT_SKIP_ORPHAN_KILL=1
  export IG_AGENT_FROM_LAUNCHER=1
  export IG_API_PORT=9090
  export IG_COCKPIT_PORT=9191
  export IG_AGENT_DATA_DIR="${ISOLATED_ROOT}/data"
  export IG_ANALYTICS_DB="${ISOLATED_ROOT}/analytics/triage_v30.db"
  export IG_TRIAGE_DB="${ISOLATED_ROOT}/analytics/triage_v30.db"
  export PYTHONPATH="${AGENT_DIR}/src"
  "${AGENT_DIR}/.venv/bin/python3" "${AGENT_DIR}/src/main.py" >>"${ISOLATED_ROOT}/data/logs/shadow_v30.log" 2>&1 &
  echo $! >"/tmp/apex_pilot_sidecar.pid"
  sleep 3
}

shadow_cleanup() {
  log "shadow_cleanup: evict stale shadow processes (production :8080 protected)"
  osascript -e 'quit app "IG Agent Apex"' 2>/dev/null || true
  sleep 1
  if command -v lsof >/dev/null 2>&1; then
    for pid in $(lsof -tiTCP:9090 -sTCP:LISTEN 2>/dev/null || true); do
      cmd=$(ps -p "${pid}" -o command= 2>/dev/null || true)
      if [[ "${cmd}" == *"main.py"* ]]; then
        log "shadow_cleanup: SIGTERM pid=${pid} on :9090"
        kill -TERM "${pid}" 2>/dev/null || true
      fi
    done
    sleep 2
    for pid in $(lsof -tiTCP:9191 -sTCP:LISTEN 2>/dev/null || true); do
      kill -TERM "${pid}" 2>/dev/null || true
    done
  fi
  bash "${AGENT_DIR}/scripts/apex-shadow-purge.sh" >>"${PILOT_LOG}" 2>&1 || true
  rm -f "${ISOLATED_ROOT}/data/.ig_agent_v30_shadow.lock" 2>/dev/null || true
  rm -f "${ISOLATED_ROOT}/data/apex_ipc.sock" 2>/dev/null || true
}

launch_apex_desktop() {
  if [[ ! -d "${APP_BUNDLE}" ]]; then
    log "FATAL: missing ${APP_BUNDLE} — run npm run apex:release first"
    exit 1
  fi
  log "launch: open IG Agent Apex.app (shadow sidecar :9090 + micro-kernel)"
  open "${APP_BUNDLE}"
  log "launch: pause 3s for worker thread stabilization"
  sleep 3
}

poll_shadow_ready() {
  local max_polls=150
  local i code body
  for i in $(seq 1 "${max_polls}"); do
    code=$(shadow_health)
    if [[ "${code}" == "200" ]]; then
      body=$(curl -s "http://127.0.0.1:9090/api/startup/status" 2>/dev/null || echo "{}")
      if [[ "${body}" == *'"ready":true'* ]] || [[ "${body}" == *'"ready": true'* ]]; then
        log "READY at t=$((i * 2))s"
        echo "${body}" | head -c 500 | tee -a "${PILOT_LOG}"
        echo | tee -a "${PILOT_LOG}"
        return 0
      fi
      log "t=$((i * 2))s health=200 boot_in_progress"
    else
      log "t=$((i * 2))s health=${code}"
    fi
    sleep 2
  done
  return 1
}

record_repair() {
  local reason="$1"
  echo "{\"ts\":\"$(date -Iseconds)\",\"reason\":$(python3 -c "import json; print(json.dumps('${reason}'))")}" >>"${REPAIR_LOG}"
}

run_pillar_verify() {
  export IG_API_PORT=9090
  export IG_AGENT_DATA_DIR="${ISOLATED_ROOT}/data"
  export IG_TRIAGE_DB="${ISOLATED_ROOT}/analytics/triage_v30.db"
  export PYTHONPATH="${AGENT_DIR}/src"
  "${AGENT_DIR}/.venv/bin/python3" "${AGENT_DIR}/scripts/apex_pillar_verify.py"
}

log "=== v30 Apex Live Pilot ==="
log "Pre-audit: production :8080=$(production_health) | shadow :9090=$(shadow_health)"

attempt=1
success=0
while [[ "${attempt}" -le "${MAX_ATTEMPTS}" ]]; do
  log "--- Attempt ${attempt}/${MAX_ATTEMPTS} ---"
  shadow_cleanup
  launch_apex_desktop

  ready=0
  if poll_shadow_ready; then
    ready=1
  else
    record_repair "electron_sidecar_timeout_attempt_${attempt}"
    start_shadow_sidecar_dev
    if poll_shadow_ready; then
      ready=1
    fi
  fi

  if [[ "${ready}" == "1" ]]; then
    if run_pillar_verify | tee -a "${PILOT_LOG}"; then
      success=1
      break
    fi
    record_repair "pillar_verify_failed_attempt_${attempt}"
    log "REPAIR: pillar verification failed — retrying launch sequence"
  else
    record_repair "boot_timeout_attempt_${attempt}"
    log "REPAIR: boot timeout — tail shadow log"
    tail -30 "${ISOLATED_ROOT}/data/logs/shadow_v30.log" 2>/dev/null | tee -a "${PILOT_LOG}" || true
  fi
  attempt=$((attempt + 1))
done

log "=== Pilot complete success=${success} ==="
log "Repairs log: ${REPAIR_LOG}"
log "Full pilot log: ${PILOT_LOG}"

if [[ "${success}" != "1" ]]; then
  exit 1
fi
exit 0
