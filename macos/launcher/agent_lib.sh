#!/bin/bash
# Shared helpers for IG Agent macOS supervisor scripts.
set -euo pipefail

_agent_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export IG_AGENT_ROOT="$(cd "${_agent_lib_dir}/../.." && pwd)"
cd "${IG_AGENT_ROOT}"

export PATH="${IG_AGENT_ROOT}/.venv/bin:${PATH}"
export PYTHONPATH="${IG_AGENT_ROOT}/src"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_API_PORT="${IG_API_PORT:-8080}"
export PORT="${IG_API_PORT}"

if [[ ! -x "${IG_AGENT_ROOT}/.venv/bin/python3" ]]; then
  export IG_AGENT_PY="$(command -v python3 || true)"
else
  export IG_AGENT_PY="${IG_AGENT_ROOT}/.venv/bin/python3"
fi

mkdir -p "${IG_AGENT_ROOT}/logs"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

resolve_data_root() {
  "${IG_AGENT_PY}" - <<'PY'
import os
from runtime.app_mode import parse_app_mode, resolve_data_root
print(resolve_data_root(parse_app_mode(os.environ["APP_MODE"])))
PY
}

export_launch_env() {
  local mode
  mode="$(printf '%s' "${APP_MODE}" | tr '[:lower:]' '[:upper:]')"
  export APP_MODE="${mode}"
  case "${mode}" in
    DEMO)
      export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31.json}"
      export IG_BROKER_PLANE=DEMO
      ;;
    LIVE)
      export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_live_canary.json}"
      export IG_BROKER_PLANE=LIVE
      ;;
    TESTBED)
      export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_testbed.json}"
      export IG_BROKER_PLANE=MOCK
      export IG_API_PORT="${IG_API_PORT:-9199}"
      export PORT="${IG_API_PORT}"
      ;;
    *)
      echo "ERROR: invalid APP_MODE=${mode}" >&2
      return 1
      ;;
  esac
  export IG_DATA_ROOT="$(resolve_data_root)"
  mkdir -p "${IG_DATA_ROOT}/logs"
}

pids_on_port() {
  local port="$1"
  lsof -t -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true
}

wait_port_free() {
  local port="$1"
  local timeout="${2:-30}"
  local i
  for ((i = 0; i < timeout; i++)); do
    if [[ -z "$(pids_on_port "${port}")" ]]; then
      return 0
    fi
    sleep 1
  done
  [[ -z "$(pids_on_port "${port}")" ]]
}

kill_pattern() {
  local sig="$1"
  shift
  local pat
  for pat in "$@"; do
    pkill "${sig}" -f "${pat}" 2>/dev/null || true
  done
}
