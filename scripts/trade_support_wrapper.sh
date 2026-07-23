#!/bin/bash
# Runs trade support wrapper daemon — always-on open-trade supervisor.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
# Pin unified desk data root — must match session_ready / main IG_DATA_ROOT.
export IG_DATA_ROOT="${IG_DATA_ROOT:-${ROOT}/src/data/v31-production}"
export IG_AGENT_DATA_DIR="${IG_AGENT_DATA_DIR:-${IG_DATA_ROOT}}"
PY="${ROOT}/.venv/bin/python3"
if [ ! -x "${PY}" ]; then
  PY="$(command -v python3)"
fi
exec "${PY}" -m runtime.trade_support_wrapper "$@"
