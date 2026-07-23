#!/bin/bash
# Runs desk support wrapper daemon — install alongside launchd
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
export IG_DATA_ROOT="${IG_DATA_ROOT:-${ROOT}/src/data/v31-production}"
export IG_AGENT_DATA_DIR="${IG_AGENT_DATA_DIR:-${IG_DATA_ROOT}}"
PY="${ROOT}/.venv/bin/python3"
if [ ! -x "${PY}" ]; then
  PY="$(command -v python3)"
fi
# launchd KeepAlive can SIGKILL a prior instance while the new job starts;
# wait so port/process probes do not race a still-shutting-down peer.
sleep 3
exec "${PY}" -m runtime.desk_support_wrapper "$@"
