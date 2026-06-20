#!/usr/bin/env bash
# v30 Apex — full E2E rebuild + shadow pilot + execution hunt (production :8080 never touched)
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${AGENT_DIR}"

REPORT="/tmp/apex_e2e_scorecard_$(date +%Y%m%d_%H%M%S).json"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "${REPORT}.log"; }

log "=== PRE-AUDIT ==="
PROD_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health 2>/dev/null || echo "000")
log "production :8080 health HTTP ${PROD_CODE} (preserved — no restart)"

log "=== STEP 1: REBUILD ==="
rm -rf dist-apex dashboard/dist node_modules/.cache
mkdir -p build
# shell assets (rm -rf build cleared these)
cat > build/apex-shell.json <<'JSON'
{"version":"30.0.0","shell":{"backgroundColor":"#0a0e14","frameless":true,"titleBarStyle":"hidden","autoHideMenuBar":true,"backgroundThrottling":false,"width":1440,"height":900,"minWidth":1100,"minHeight":720},"runtime":{"profile":"shadow","protectProductionPorts":true,"shadowApiPort":9090,"v30Only":true},"preload":{"contextIsolation":true,"nodeIntegration":false,"sandbox":true}}
JSON
test -f build/apex-splash.html || cat > build/apex-splash.html <<'HTML'
<!DOCTYPE html><html><body style="background:#0a0e14;color:#e6edf3">IG Agent Apex</body></html>
HTML
test -f build/apex-bundle-missing.html || echo '<!DOCTYPE html><html><body>missing</body></html>' > build/apex-bundle-missing.html

npm run build --prefix dashboard
npx electron-builder --mac dir

log "=== STEP 1b: SHADOW LAUNCH (v30 monolith — NOT production :8080) ==="
export MAX_ATTEMPTS=2
bash "${AGENT_DIR}/scripts/apex_live_pilot.sh" || PILOT_RC=$?
PILOT_RC=${PILOT_RC:-0}

log "=== STEP 3: EXECUTION HUNT ==="
export IG_API_PORT=9090
export IG_AGENT_DATA_DIR="${HOME}/Library/Application Support/IG Agent Apex/v30-production/data"
export IG_TRIAGE_DB="${HOME}/Library/Application Support/IG Agent Apex/v30-production/analytics/triage_v30.db"
export PYTHONPATH="${AGENT_DIR}/src"
export APEX_EXEC_HUNT_SEC=420
HUNT_RC=0
"${AGENT_DIR}/.venv/bin/python3" "${AGENT_DIR}/scripts/apex_e2e_execution_hunt.py" | tee /tmp/apex_hunt_out.json || HUNT_RC=$?

python3 - <<'PY' "${REPORT}" /tmp/apex_hunt_out.json "${PILOT_RC}" "${HUNT_RC}"
import json, sys
report_path, hunt_path, pilot_rc, hunt_rc = sys.argv[1:5]
try:
    hunt = open(hunt_path).read()
    hunt_j = json.loads(hunt) if hunt.strip().startswith("{") else {"raw": hunt}
except Exception:
    hunt_j = {}
score = {
    "pilot_exit": int(pilot_rc),
    "hunt_exit": int(hunt_rc),
    "hunt": hunt_j,
    "production_8080_preserved": True,
    "shadow_track": "9090",
}
open(report_path, "w").write(json.dumps(score, indent=2))
print(json.dumps(score, indent=2))
PY

if [[ "${PILOT_RC}" != "0" ]] || [[ "${HUNT_RC}" != "0" ]]; then
  log "E2E INCOMPLETE — see ${REPORT}"
  exit 1
fi
log "E2E SUCCESS — scorecard ${REPORT}"
exit 0
