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
  alert_launcher "Health check failed"
  exit 1
fi

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
    log "gui_status partial — missing: ${missing[*]}"
  fi
  sleep "${POLL}"
done

if (( gui_ok != 1 )); then
  log "ERROR: gui_status not ready; missing=${missing[*]:-all}"
  if ! "${IG_AGENT_PY}" "${SCRIPT_DIR}/launcher_core.py" --phase verify --port "${PORT}" 2>/dev/null; then
    alert_launcher "GUI verification failed"
    exit 1
  fi
fi

ROUTE_COUNT="$("${IG_AGENT_PY}" -c "
import json
print(len(json.load(open('/tmp/ig_verify_gui.json')).get('unified_execution_route') or []))
" 2>/dev/null || echo "0")"
log "agent_verify complete — unified_execution_route count=${ROUTE_COUNT}"
exit 0
