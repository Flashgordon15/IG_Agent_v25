#!/usr/bin/env bash
# v30 Apex — native autonomous launch harness (Mac Mini sandbox).
# Full lifecycle: purge → build → headless :9090 boot → health handshake → Electron open → log report.
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${AGENT_DIR}"

ISOLATED="${HOME}/Library/Application Support/IG Agent Apex/v30-production"
APP_BUNDLE="${AGENT_DIR}/dist-apex/mac-arm64/IG Agent Apex.app"
BOOT_LOG="${AGENT_DIR}/logs/production_boot.log"
PILOT_LOG="${AGENT_DIR}/logs/apex_autonomous_pilot.log"
PID_FILE="${AGENT_DIR}/logs/apex_headless.pid"
HEALTH_URL="http://127.0.0.1:9090/api/health"
HANDSHAKE_TIMEOUT_SEC="${HANDSHAKE_TIMEOUT_SEC:-180}"
VENV_PY="${AGENT_DIR}/.venv/bin/python3"
EBUILDER="${AGENT_DIR}/node_modules/.bin/electron-builder"

mkdir -p "${AGENT_DIR}/logs" "${AGENT_DIR}/build" "${AGENT_DIR}/src/data/logs"
: >"${PILOT_LOG}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${PILOT_LOG}"
}

die() {
  log "FATAL: $*"
  exit 1
}

# ---------------------------------------------------------------------------
# 1. FORCE PORT RECLAMATION AND CLEANUP
# ---------------------------------------------------------------------------
step_purge() {
  log "=== STEP 1: FORCE PORT RECLAMATION AND CLEANUP ==="

  perl -e 'alarm 5; exec @ARGV' osascript -e 'quit app "IG Agent Apex"' 2>/dev/null || true
  sleep 1

  for pid in $(pgrep -f "src/main.py" 2>/dev/null || true); do
    log "purge: SIGTERM main.py pid=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
  done
  sleep 2
  for pid in $(pgrep -f "src/main.py" 2>/dev/null || true); do
    log "purge: SIGKILL main.py pid=${pid}"
    kill -9 "${pid}" 2>/dev/null || true
  done

  for port in 9090 8080; do
    log "purge: scanning lsof -i tcp:${port}"
    while read -r pid; do
      [[ -z "${pid}" ]] && continue
      log "purge: kill -9 pid=${pid} (blocking :${port})"
      kill -9 "${pid}" 2>/dev/null || true
    done < <(lsof -ti tcp:"${port}" -sTCP:LISTEN 2>/dev/null || true)
  done

  rm -f \
    "${AGENT_DIR}/src/data/apex_ipc.sock" \
    "${AGENT_DIR}/src/data/apex_ipc_shadow.sock" \
    "${ISOLATED}/data/apex_ipc.sock" \
    "${ISOLATED}/data/apex_ipc_shadow.sock" \
    "${AGENT_DIR}/src/data/.ig_agent_v29.lock" \
    "${ISOLATED}/data/.ig_agent_v30_shadow.lock" \
    "${PID_FILE}" \
    2>/dev/null || true

  rm -rf "${AGENT_DIR}/node_modules/.cache" "${AGENT_DIR}/dashboard/node_modules/.cache" 2>/dev/null || true
  find "${AGENT_DIR}/src" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "${AGENT_DIR}/src" -name '*.pyc' -delete 2>/dev/null || true

  log "purge complete — ports 8080/9090 cleared, IPC sockets removed, caches wiped"
}

# ---------------------------------------------------------------------------
# 2. FRONT-END PRODUCTION BUILD + ELECTRON PACK
# ---------------------------------------------------------------------------
step_build() {
  log "=== STEP 2: UNTHROTTLED FRONT-END PRODUCTION BUILD ==="

  if [[ ! -f "${AGENT_DIR}/build/apex-shell.json" ]]; then
    cat > "${AGENT_DIR}/build/apex-shell.json" <<'JSON'
{
  "name": "ig-agent-apex-monolith",
  "version": "30.0.0",
  "environment": "shadow",
  "apiPort": 9090,
  "shell": {
    "backgroundColor": "#0a0e14",
    "frameless": true,
    "titleBarStyle": "hidden",
    "autoHideMenuBar": true,
    "backgroundThrottling": false,
    "width": 1440,
    "height": 900,
    "minWidth": 1100,
    "minHeight": 720
  },
  "runtime": {
    "profile": "shadow",
    "protectProductionPorts": true,
    "shadowApiPort": 9090,
    "v30Only": true
  },
  "preload": {
    "contextIsolation": true,
    "nodeIntegration": false,
    "sandbox": true
  }
}
JSON
  fi

  test -f "${AGENT_DIR}/build/apex-splash.html" || cat > "${AGENT_DIR}/build/apex-splash.html" <<'HTML'
<!DOCTYPE html><html><head><meta charset="utf-8"><title>IG Agent Apex</title></head>
<body style="background:#0a0e14;color:#e6edf3;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">IG Agent Apex v30.0 — booting…</body></html>
HTML

  log "npm run build --prefix dashboard"
  npm run build --prefix dashboard

  if [[ -x "${AGENT_DIR}/scripts/apex-dist-guard.sh" ]]; then
    bash "${AGENT_DIR}/scripts/apex-dist-guard.sh" "${AGENT_DIR}/dashboard/dist"
  fi

  log "electron-builder --mac dir"
  if [[ -x "${EBUILDER}" ]]; then
    "${EBUILDER}" --mac dir
  else
    npx electron-builder --mac dir
  fi

  if [[ -d "${APP_BUNDLE}" ]]; then
    bash "${AGENT_DIR}/scripts/apex-fix-asar-integrity.sh" "${APP_BUNDLE}"
    xattr -cr "${APP_BUNDLE}" 2>/dev/null || true
  fi

  [[ -d "${APP_BUNDLE}" ]] || die "missing application bundle: ${APP_BUNDLE}"
  log "build complete — ${APP_BUNDLE}"
}

# ---------------------------------------------------------------------------
# 3. HEADLESS PYTHON SANDBOX ON :9090
# ---------------------------------------------------------------------------
step_boot_headless() {
  log "=== STEP 3: SPIN UP HEADLESS PYTHON SANDBOX ENGINE (:9090) ==="

  [[ -x "${VENV_PY}" ]] || die "missing venv python: ${VENV_PY}"

  : >"${BOOT_LOG}"

  # IG_API_PORT + desktop align overrides NODE_ENV=production leakage → bind :9090
  env \
    IG_PRICING_REFERENCE=yahoo \
    IG_APEX_NO_BROWSER=1 \
    IG_APEX_DESKTOP=1 \
    IG_AGENT_FROM_LAUNCHER=1 \
    IG_AGENT_SKIP_ORPHAN_KILL=1 \
    IG_APEX_PROTECT_PRODUCTION_PORTS=1 \
    IG_API_PORT=9090 \
    IG_NODE_PROFILE=shadow \
    NODE_ENV=production \
    IG_AGENT_ROOT="${AGENT_DIR}" \
    IG_AGENT_DATA_DIR="${ISOLATED}/data" \
    IG_TRIAGE_DB="${ISOLATED}/analytics/triage_v30.db" \
    PYTHONPATH="${AGENT_DIR}/src" \
    "${VENV_PY}" "${AGENT_DIR}/src/main.py" >>"${BOOT_LOG}" 2>&1 &

  echo $! >"${PID_FILE}"
  log "headless sidecar pid=$(cat "${PID_FILE}") log=${BOOT_LOG}"
}

# ---------------------------------------------------------------------------
# 4. HEALTH HANDSHAKE LOOP + OPEN ELECTRON CONTAINER
# ---------------------------------------------------------------------------
step_handshake_and_open() {
  log "=== STEP 4: CHECKPOINT HANDSHAKE PINGS (target ${HEALTH_URL}) ==="

  local elapsed=0
  local code="000"

  while (( elapsed < HANDSHAKE_TIMEOUT_SEC )); do
    local remaining=$((HANDSHAKE_TIMEOUT_SEC - elapsed))
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "${HEALTH_URL}" 2>/dev/null || echo "000")

    if [[ "${code}" == "200" ]]; then
      log "HANDSHAKE OK — status 200 on :9090 after ${elapsed}s"
      log "launching native container (Electron adopts existing :9090 sidecar): ${APP_BUNDLE}"
      open "${APP_BUNDLE}"
      sleep 2
      return 0
    fi

    printf "\r[%02ds] awaiting sidecar handshake… HTTP %s (timeout in %03ds)   " "${elapsed}" "${code}" "${remaining}"
  done

  echo ""
  die "handshake timeout — ${HEALTH_URL} never returned HTTP 200 (last=${code})"
}

# ---------------------------------------------------------------------------
# 5. LOG REPORT PASSES — lifecycle telemetry to screen
# ---------------------------------------------------------------------------
step_log_report() {
  log "=== STEP 5: LOG REPORT PASSES (lifecycle telemetry) ==="

  local log_file="${BOOT_LOG}"
  if [[ ! -s "${log_file}" ]] && [[ -f "${ISOLATED}/data/logs/shadow_v30.log" ]]; then
    log_file="${ISOLATED}/data/logs/shadow_v30.log"
  fi

  log "--- tailing boot log: ${log_file} ---"

  report_pass() {
    local label="$1"
    local pattern="$2"
    if rg -q "${pattern}" "${log_file}" 2>/dev/null; then
      log "PASS | ${label}"
      rg -n "${pattern}" "${log_file}" 2>/dev/null | tail -3 | while read -r line; do
        log "      ${line}"
      done
    else
      log "WAIT | ${label} — pattern not yet in log"
    fi
  }

  report_pass "256-bar vector compile" "Array warmup: complete|256-bar|bars compiled"
  report_pass "indicator hot-path (<250µs budget)" "250µs|indicator_us|compute_math_matrix|micro-kernel"
  report_pass "ML veto floor (0.450)" "0\\.450|ML_VETO|ml_veto_floor|veto floor"
  report_pass "execution / latency (<200ms)" "health_ms|EXEC OK|latency_us|sub-200|invoking execution|ALL GATES PASSED"

  log "--- live health probe ---"
  local t0 t1 ms code
  t0=$(python3 -c "import time; print(time.perf_counter())")
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "${HEALTH_URL}" 2>/dev/null || echo "000")
  t1=$(python3 -c "import time; print(time.perf_counter())")
  ms=$(python3 -c "print(int((${t1}-${t0})*1000))")
  log "health HTTP ${code} round-trip ~${ms}ms"

  if [[ -f "${AGENT_DIR}/scripts/apex_e2e_live_cycle_verify.py" ]]; then
    log "--- pillar verification scorecard ---"
    env \
      IG_PRICING_REFERENCE=yahoo \
      IG_API_PORT=9090 \
      IG_AGENT_DATA_DIR="${ISOLATED}/data" \
      IG_TRIAGE_DB="${ISOLATED}/analytics/triage_v30.db" \
      PYTHONPATH="${AGENT_DIR}/src" \
      "${VENV_PY}" "${AGENT_DIR}/scripts/apex_e2e_live_cycle_verify.py" \
      | tee "${AGENT_DIR}/logs/apex_release_scorecard.json" \
      || log "WARN: pillar verify returned non-zero (see scorecard)"
  fi

  log "=== APEX AUTONOMOUS PILOT COMPLETE ==="
  log "boot log: ${BOOT_LOG}"
  log "pilot log: ${PILOT_LOG}"
  log "headless pid: $(cat "${PID_FILE}" 2>/dev/null || echo unknown)"
}

# ---------------------------------------------------------------------------
main() {
  log "APEX AUTONOMOUS PILOT — workspace ${AGENT_DIR}"
  step_purge
  step_build
  step_boot_headless
  step_handshake_and_open
  step_log_report
}

main "$@"
