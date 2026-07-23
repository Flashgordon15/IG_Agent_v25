#!/usr/bin/env bash
# Pause / resume NEW entries while keeping open-position supervision alive.
#
# Preferred path for hotfixes when the book may be OPEN:
#   ./scripts/desk_dev_pause.sh pause     # freeze entries; OPM + trade_support keep watching
#   ./scripts/desk_dev_pause.sh resume    # clear holds
#   ./scripts/desk_dev_pause.sh status
#
# Does NOT flatten. Does NOT stop main / trade_support. Never kill -9.
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

case "${CMD}" in
  pause|on)
    "${PY}" -c "
from runtime.desk_dev_controls import pause_entries, status_snapshot
import json
print(json.dumps(pause_entries(reason='${REASON}'), indent=2, default=str))
print('---')
print(json.dumps(status_snapshot(), indent=2, default=str))
"
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
    echo "RESUMED: entry holds cleared"
    ;;
  status)
    "${PY}" -c "
from runtime.desk_dev_controls import status_snapshot
import json
print(json.dumps(status_snapshot(), indent=2, default=str))
"
    ;;
  -h|--help|help)
    sed -n '2,12p' "$0"
    ;;
  *)
    echo "Unknown command: ${CMD} (pause|resume|status)" >&2
    exit 1
    ;;
esac
