#!/usr/bin/env bash
# Background monitor — polls dashboard /state and logs changes.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${ROOT}/src/data/logs/monitor.log"
INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-60}"
API="http://127.0.0.1:8080/state"
LAST_FILE="${ROOT}/src/data/logs/.monitor_last.json"

mkdir -p "${ROOT}/src/data/logs"
: >> "${LOG}"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${LOG}"
}

fetch_state() {
  curl -sf --max-time 5 "${API}" 2>/dev/null || echo ""
}

summary_from_json() {
  PYTHONPATH="${ROOT}/src" python3 - "$1" <<'PY'
import json, sys
raw = sys.argv[1]
if not raw.strip():
    print("OFFLINE")
    sys.exit(0)
try:
    d = json.loads(raw)
except json.JSONDecodeError:
    print("BAD_JSON")
    sys.exit(0)
h = d.get("health") or {}
gates = h.get("gates") or []
passing = sum(1 for g in gates if g.get("pass"))
total = len(gates) or 7
cold = next((g for g in gates if g.get("name") == "cold_start_gap"), {})
sig = next((g for g in gates if g.get("name") == "signal_confidence"), {})
badge = h.get("badge", "?")
summary = h.get("summary", "")[:80]
print(
    f"badge={badge} gates={passing}/{total} "
    f"bid={d.get('bid')} stream={d.get('stream_status')} tick_age={d.get('tick_age_s')} "
    f"cold={cold.get('detail','—')[:40]} sig={sig.get('detail','—')[:35]} "
    f"| {summary}"
)
PY
}

log "=== monitor start interval=${INTERVAL_SEC}s pid=$$ ==="

PREV=""
while true; do
  RAW="$(fetch_state)"
  SUM="$(summary_from_json "${RAW}")"
  if [[ "${SUM}" == "OFFLINE" ]]; then
    log "ALERT agent offline (no /state)"
    echo "AGENT_MONITOR_ALERT agent offline — no response from :8080/state"
  elif [[ "${SUM}" != "${PREV}" ]] && [[ -n "${PREV}" ]]; then
    log "CHANGE ${SUM}"
    echo "AGENT_MONITOR_CHANGE ${SUM}"
  elif [[ -z "${PREV}" ]]; then
    log "BASELINE ${SUM}"
  fi
  PREV="${SUM}"
  sleep "${INTERVAL_SEC}"
done
