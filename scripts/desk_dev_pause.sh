#!/usr/bin/env bash
# Pause / resume NEW entries while keeping open-position supervision alive.
#
# Preferred path for hotfixes when the book may be OPEN:
#   ./scripts/desk_dev_pause.sh pause     # freeze entries; OPM + trade_support keep watching
#   ./scripts/desk_dev_pause.sh resume    # clear holds
#   ./scripts/desk_dev_pause.sh status
#
# Does NOT flatten. Does NOT stop main / trade_support. Never kill -9.
# Dual-port: disk flags write to state/ + state_cfd/ + state_sb/, then
# POST /api/stop|/api/start on :8080 and :8081 so process trading_paused matches health.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
PY="${ROOT}/.venv/bin/python3"
if [ ! -x "${PY}" ]; then
  PY="$(command -v python3)"
fi

CMD="${1:-status}"
REASON="${2:-desk_dev_pause}"
CFD_PORT="${IG_API_PORT_CFD:-8080}"
SB_PORT="${IG_API_PORT_SB:-8081}"

_post_ports() {
  local path="$1"
  local label="$2"
  for port in "${CFD_PORT}" "${SB_PORT}"; do
    if curl -sf --max-time 3 -X POST "http://127.0.0.1:${port}${path}" >/dev/null 2>&1; then
      echo "  :${port} ${label} OK"
    else
      echo "  :${port} ${label} skipped/unreachable (disk flags still written)" >&2
    fi
  done
}

case "${CMD}" in
  pause|on)
    "${PY}" -c "
from runtime.desk_dev_controls import pause_entries, status_snapshot
import json
print(json.dumps(pause_entries(reason='${REASON}'), indent=2, default=str))
print('---')
print(json.dumps(status_snapshot(), indent=2, default=str))
"
    echo "Syncing process pause via POST /api/stop (dual-port)..."
    _post_ports "/api/stop" "stop"
    echo "PAUSED: new entries frozen — trade_support/OPM still supervise opens"
    ;;
  resume|off|clear)
    "${PY}" -c "
from runtime.desk_dev_controls import resume_entries, status_snapshot
import json
print(json.dumps(resume_entries(reason='${REASON:-desk_dev_resume}'), indent=2, default=str))
print('---')
print(json.dumps(status_snapshot(), indent=2, default=str))
"
    # Do NOT auto-start CFD when A2 hard_block is active — only clear disk holds.
    # Operator unlock is explicit POST /api/start on the intended port(s).
    echo "Disk holds cleared. Process start left to operator (A2 CFD hard_block preserved)."
    echo "RESUMED: entry holds cleared (process loops unchanged unless you POST /api/start)"
    ;;
  status)
    "${PY}" -c "
from runtime.desk_dev_controls import status_snapshot
import json
print(json.dumps(status_snapshot(), indent=2, default=str))
"
    ;;
  -h|--help|help)
    sed -n '2,14p' "$0"
    ;;
  *)
    echo "Unknown command: ${CMD} (pause|resume|status)" >&2
    exit 1
    ;;
esac
