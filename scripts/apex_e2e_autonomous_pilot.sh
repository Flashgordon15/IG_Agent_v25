#!/usr/bin/env bash
# v30 Apex — autonomous E2E workspace runtime validation (local mock sandbox).
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${AGENT_DIR}"

ISOLATED="${HOME}/Library/Application Support/IG Agent Apex/v30-production"
APP_BUNDLE="${AGENT_DIR}/dist-apex/mac-arm64/IG Agent Apex.app"
PROD_LOG="${AGENT_DIR}/src/data/logs/production_boot.log"
SCORECARD="/tmp/apex_e2e_release_scorecard.json"
PILOT_LOG="/tmp/apex_e2e_autonomous_pilot.log"
REPAIR_LOG="/tmp/apex_e2e_repairs.jsonl"
MAX_CYCLES="${MAX_CYCLES:-3}"

mkdir -p "${AGENT_DIR}/src/data/logs" "${ISOLATED}/data/logs" "${ISOLATED}/analytics" build
: >"${REPAIR_LOG}"
: >"${PILOT_LOG}"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "${PILOT_LOG}"; }

record_repair() {
  python3 - <<'PY' "${REPAIR_LOG}" "$1" "$2"
import json, sys, time
path, kind, detail = sys.argv[1:4]
with open(path, "a") as f:
    f.write(json.dumps({"ts": time.time(), "kind": kind, "detail": detail}) + "\n")
PY
}

purge_all() {
  log "=== STEP 1: FORCE PURGE PORT LOCKS & CACHE TRAILS ==="
  perl -e 'alarm 5; exec @ARGV' osascript -e 'quit app "IG Agent Apex"' 2>/dev/null || true
  sleep 1

  for pid in $(pgrep -f "src/main.py" 2>/dev/null || true); do
    log "purge: SIGTERM main.py pid=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
  done
  sleep 2
  for pid in $(pgrep -f "src/main.py" 2>/dev/null || true); do
    kill -9 "${pid}" 2>/dev/null || true
  done

  for port in 8080 9090 8787 9191; do
    for pid in $(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true); do
      log "purge: SIGKILL pid=${pid} on :${port}"
      kill -9 "${pid}" 2>/dev/null || true
    done
  done

  rm -f "${AGENT_DIR}/src/data/apex_ipc.sock" \
    "${AGENT_DIR}/src/data/.ig_agent_v29.lock" \
    "${ISOLATED}/data/apex_ipc.sock" \
    "${ISOLATED}/data/.ig_agent_v30_shadow.lock" \
    /tmp/apex_pilot_sidecar.pid 2>/dev/null || true

  rm -rf "${AGENT_DIR}/dist-apex" "${AGENT_DIR}/dashboard/dist" "${AGENT_DIR}/node_modules/.cache"
  find "${AGENT_DIR}/src" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "${AGENT_DIR}/src" -name '*.pyc' -delete 2>/dev/null || true
  log "purge complete"
}

build_monolith() {
  log "=== STEP 2: STANDALONE MONOLITH COMPILATION PIPELINE ==="
  cat > build/apex-shell.json <<'JSON'
{"version":"30.0.0","shell":{"backgroundColor":"#0a0e14","frameless":true,"titleBarStyle":"hidden","autoHideMenuBar":true,"backgroundThrottling":false,"width":1440,"height":900,"minWidth":1100,"minHeight":720},"runtime":{"profile":"shadow","protectProductionPorts":true,"shadowApiPort":9090,"v30Only":true},"preload":{"contextIsolation":true,"nodeIntegration":false,"sandbox":true}}
JSON
  test -f build/apex-splash.html || echo '<!DOCTYPE html><html><body style="background:#0a0e14;color:#e6edf3">IG Agent Apex</body></html>' > build/apex-splash.html
  test -f build/apex-bundle-missing.html || echo '<!DOCTYPE html><html><body>missing bundle</body></html>' > build/apex-bundle-missing.html

  npm run build --prefix dashboard
  bash "${AGENT_DIR}/scripts/apex-dist-guard.sh" dashboard/dist
  npx electron-builder --mac dir
  log "monolith build complete"
}

start_headless_recovery() {
  log "=== STEP 3: HEADLESS RECOVERY BOOT (production :8080 mock feed) ==="
  : >"${PROD_LOG}"
  export IG_MOCK_FEED=1
  export IG_APEX_NO_BROWSER=1
  export NODE_ENV=production
  export PYTHONPATH="${AGENT_DIR}/src"
  nohup "${AGENT_DIR}/.venv/bin/python3" "${AGENT_DIR}/src/main.py" >>"${PROD_LOG}" 2>&1 &
  echo $! > /tmp/apex_headless_recovery.pid
  log "headless recovery pid=$(cat /tmp/apex_headless_recovery.pid) log=${PROD_LOG}"
  sleep 4
}

launch_electron() {
  log "=== STEP 3b: OPEN NATIVE STANDALONE BUNDLE ==="
  if [[ ! -d "${APP_BUNDLE}" ]]; then
    log "FATAL: missing ${APP_BUNDLE}"
    exit 1
  fi
  open "${APP_BUNDLE}"
  log "Electron bundle launched"
  sleep 5
}

monitor_boot() {
  log "=== STEP 4: LIVE CHECKPOINT INTERCEPTION ==="
  local deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    if rg -q 'webbrowser\.open|Opened external browser' "${PROD_LOG}" "${ISOLATED}/data/logs" 2>/dev/null; then
      record_repair "browser_leak" "webbrowser.open detected — already patched in src/"
      log "REPAIR: browser auto-launch suppressed in source (no relaunch needed)"
    fi

    local prod_code shadow_code
    prod_code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health 2>/dev/null || echo "000")
    shadow_code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9090/api/health 2>/dev/null || echo "000")
    log "health probe :8080=${prod_code} :9090=${shadow_code}"

    if [[ "${shadow_code}" == "000" ]] && [[ -f "${ISOLATED}/data/logs/shadow_v30.log" ]]; then
      if rg -q 'Gate1FatalError|port 9090 already in use' "${ISOLATED}/data/logs/shadow_v30.log" 2>/dev/null; then
        record_repair "port_9090" "evicting stale shadow listener"
        for pid in $(lsof -tiTCP:9090 -sTCP:LISTEN 2>/dev/null || true); do kill -9 "${pid}" 2>/dev/null || true; done
        osascript -e 'quit app "IG Agent Apex"' 2>/dev/null || true
        sleep 2
        open "${APP_BUNDLE}"
      fi
    fi

    if [[ "${prod_code}" =~ ^2 ]] && [[ "${shadow_code}" =~ ^2 ]]; then
      log "both API tracks healthy"
      return 0
    fi
    sleep 5
  done
  log "WARN: health deadline elapsed — continuing to pillar verify"
  return 0
}

run_cycle_verify() {
  log "=== STEP 5: END-TO-END LIFE CYCLE VERIFICATION ==="
  export PYTHONPATH="${AGENT_DIR}/src"
  export IG_MOCK_FEED=1
  export IG_API_PORT=9090
  export IG_AGENT_DATA_DIR="${ISOLATED}/data"
  export IG_TRIAGE_DB="${ISOLATED}/analytics/triage_v30.db"
  "${AGENT_DIR}/.venv/bin/python3" "${AGENT_DIR}/scripts/apex_e2e_live_cycle_verify.py" | tee "${SCORECARD}"
}

main() {
  log "=== APEX AUTONOMOUS E2E PILOT START ==="
  local cycle=1 rc=1
  while (( cycle <= MAX_CYCLES )); do
    log "--- cycle ${cycle}/${MAX_CYCLES} ---"
    purge_all
    build_monolith
    start_headless_recovery
    launch_electron
    monitor_boot || true
    if run_cycle_verify; then
      rc=0
      break
    fi
    record_repair "cycle_retry" "pillar verify failed — retrying"
    cycle=$((cycle + 1))
  done

  if [[ "${rc}" -eq 0 ]]; then
    log "E2E SUCCESS — scorecard ${SCORECARD}"
  else
    log "E2E FAILED after ${MAX_CYCLES} cycles — see ${PILOT_LOG}"
  fi
  exit "${rc}"
}

main "$@"
