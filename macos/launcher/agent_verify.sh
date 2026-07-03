#!/bin/bash
# Poll /api/gui_status until v31–v41 fields present; clean timeout on failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"
# shellcheck source=lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

LOG="${IG_AGENT_ROOT}/logs/agent_verify.log"
exec > >(tee -a "${LOG}") 2>&1

PORT="${IG_API_PORT:-8080}"
TIMEOUT="${LAUNCHER_VERIFY_TIMEOUT_SEC:-300}"
POLL="${LAUNCHER_VERIFY_POLL_SEC:-5}"

REQUIRED_FIELDS=(
  strategy_selector_advice strategy_controller_decisions strategy_governance
  unified_execution_route hard_enforcement_decisions trade_pipeline_health
)

log "========== agent_verify begin port=${PORT} timeout=${TIMEOUT}s =========="
launcher_status_set "verify" "Stage 8 — Verification" "Checking /api/health" 8 9

health_ok=0
deadline=$(( $(date +%s) + TIMEOUT ))
while (( $(date +%s) < deadline )); do
  if curl -sf --max-time 30 "http://127.0.0.1:${PORT}/api/health" -o /tmp/ig_verify_health.json 2>/dev/null; then
    health_ok=1
    break
  fi
  sleep "${POLL}"
done

if (( health_ok != 1 )); then
  log "ERROR: /api/health unreachable within ${TIMEOUT}s"
  launcher_status_fail "Verification failed" "/api/health unreachable" 8
  alert_launcher "Health check failed"
  exit 1
fi

BOOT_TIER="$("${IG_AGENT_ROOT}/scripts/boot_acceptance.sh" --tier-only 2>/dev/null || echo red)"
log "boot_acceptance tier=${BOOT_TIER}"
if [[ "${BOOT_TIER}" == "red" ]]; then
  if ! "${IG_AGENT_ROOT}/scripts/boot_acceptance.sh" 2>&1; then
    launcher_status_fail "Verification failed" "Boot contract red — API not ready" 8
    alert_launcher "Boot acceptance failed"
    exit 1
  fi
fi
if [[ "${BOOT_TIER}" == "amber" ]]; then
  launcher_status_set "verify" "Stage 8 — Verification" "Degraded boot — continuing checks" 8 9 "" "amber"
fi

launcher_status_set "verify" "Stage 8 — Verification" "Checking /api/gui_status fields" 8 9
gui_ok=0
missing=()
while (( $(date +%s) < deadline )); do
  if curl -sf --max-time 45 "http://127.0.0.1:${PORT}/api/gui_status" -o /tmp/ig_verify_gui.json 2>/dev/null; then
    missing=()
    for f in "${REQUIRED_FIELDS[@]}"; do
      if ! "${IG_AGENT_PY}" -c "
import json, sys
d=json.load(open('/tmp/ig_verify_gui.json'))
sys.exit(0 if '${f}' in d else 1)
" 2>/dev/null; then
        missing+=("${f}")
      fi
    done
    if ((${#missing[@]} == 0)); then
      gui_ok=1
      break
    fi
    if "${IG_AGENT_PY}" -c "
import json, sys
d = json.load(open('/tmp/ig_verify_gui.json'))
if d.get('trading_ready') or d.get('cockpit_usable'):
    sys.exit(0)
gp = d.get('gate_progression') or {}
if gp.get('operational_ready') or str(gp.get('phase') or '').upper() in ('G5', 'READY'):
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
      log "gui_status operational — accepting partial governance snapshot (missing: ${missing[*]})"
      gui_ok=1
      break
    fi
    log "gui_status partial — missing: ${missing[*]}"
  fi
  sleep "${POLL}"
done

if (( gui_ok != 1 )); then
  log "ERROR: gui_status not ready; missing=${missing[*]:-all}"
  if ! "${IG_AGENT_PY}" "${SCRIPT_DIR}/launcher_core.py" --phase verify --port "${PORT}" 2>/dev/null; then
    launcher_status_fail "Verification failed" "gui_status missing: ${missing[*]:-all}" 8
    alert_launcher "GUI verification failed"
    exit 1
  fi
fi

ROUTE_COUNT="$("${IG_AGENT_PY}" -c "
import json
print(len(json.load(open('/tmp/ig_verify_gui.json')).get('unified_execution_route') or []))
" 2>/dev/null || echo "0")"

COCKPIT_PORT="${IG_COCKPIT_PORT:-8787}"
if [[ "${LAUNCHER_SKIP_COCKPIT_VERIFY:-}" != "1" ]]; then
  launcher_status_set "verify" "Stage 8 — Verification" "Checking Flight Deck :${COCKPIT_PORT}" 8 9
  cockpit_ok=0
  cockpit_deadline=$(( $(date +%s) + ${LAUNCHER_COCKPIT_VERIFY_SEC:-120} ))
  while (( $(date +%s) < cockpit_deadline )); do
    if curl -sf --max-time 8 "http://127.0.0.1:${COCKPIT_PORT}/api/cockpit_ready" -o /tmp/ig_verify_cockpit.json 2>/dev/null; then
      if "${IG_AGENT_PY}" -c "
import json, sys
d=json.load(open('/tmp/ig_verify_cockpit.json'))
sys.exit(0 if d.get('checks_passed', 0) >= 3 else 1)
" 2>/dev/null; then
        cockpit_ok=1
        break
      fi
    fi
  if curl -sf --max-time 5 "http://127.0.0.1:${COCKPIT_PORT}/api/orchestrator_state" >/dev/null 2>&1; then
      log "cockpit partial — orchestrator route live, readiness still warming"
    fi
    sleep "${POLL}"
  done
  if (( cockpit_ok != 1 )); then
    log "WARN: cockpit :${COCKPIT_PORT} readiness incomplete — agent API verified; UI may warm on first poll"
  else
  COCKPIT_PASSED="$("${IG_AGENT_PY}" -c "import json; print(json.load(open('/tmp/ig_verify_cockpit.json')).get('checks_passed',0))" 2>/dev/null || echo 0)"
    log "cockpit_ready ok checks_passed=${COCKPIT_PASSED}/${LAUNCHER_COCKPIT_VERIFY_SEC:-120}s window"
  fi
fi

log "agent_verify complete — unified_execution_route count=${ROUTE_COUNT}"
launcher_status_set "verify" "Stage 8 complete" "GUI verified · ${ROUTE_COUNT} routes" 8 9
exit 0
