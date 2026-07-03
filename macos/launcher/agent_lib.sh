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

# shellcheck source=launcher_status.sh
source "${_agent_lib_dir}/launcher_status.sh"

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
      export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
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
  export IG_AGENT_FROM_LAUNCHER=1
  export IG_NON_BLOCKING_BOOT=1
  if [[ "${LAUNCHER_DESKTOP:-}" == "1" ]]; then
    export IG_AGENT_DESKTOP_LAUNCH=1
    export IG_APEX_DESKTOP=1
    export LAUNCHER_BIND_DEADLINE_SEC="${LAUNCHER_BIND_DEADLINE_SEC:-180}"
    export LAUNCHER_G5_STUCK_ESCALATE_SEC="${LAUNCHER_G5_STUCK_ESCALATE_SEC:-60}"
    export LAUNCHER_G5_HYDRATION_PASS_SEC="${LAUNCHER_G5_HYDRATION_PASS_SEC:-45}"
  fi
}

port_accepts_tcp() {
  local port="$1"
  "${IG_AGENT_PY}" -c "
import socket
port = int('${port}')
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.4)
try:
    s.connect(('127.0.0.1', port))
    raise SystemExit(0)
except OSError:
    raise SystemExit(1)
finally:
    s.close()
" 2>/dev/null
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

# Fast HTTP liveness — bootstrap /health first (available before deferred routers mount).
http_probe_ok() {
  local url="$1"
  local timeout="${2:-3}"
  curl -sf --max-time "${timeout}" \
    -H "User-Agent: IG-Agent-Launcher/31" \
    "${url}" -o /dev/null 2>/dev/null
}

http_any_alive() {
  local port="$1"
  if ! port_accepts_tcp "${port}"; then
    return 1
  fi
  http_probe_ok "http://127.0.0.1:${port}/health" 1 \
    || http_probe_ok "http://127.0.0.1:${port}/api/health_light" 2 \
    || http_probe_ok "http://127.0.0.1:${port}/api/health" 3
}

kill_port_listeners() {
  local port="$1"
  local pids
  pids="$(pids_on_port "${port}")"
  [[ -z "${pids}" ]] && return 0
  log "escalating unresponsive listeners on :${port}: ${pids}"
  echo "${pids}" | xargs kill -TERM 2>/dev/null || true
  sleep 3
  pids="$(pids_on_port "${port}")"
  [[ -n "${pids}" ]] && echo "${pids}" | xargs kill -9 2>/dev/null || true
  sleep 1
}

# Reuse a healthy agent already on :PORT, or purge zombie listeners that hold the socket.
ensure_agent_port_ready() {
  local port="${PORT}"
  local pids tier
  pids="$(pids_on_port "${port}")"
  if [[ -z "${pids}" ]]; then
    return 0
  fi

  log "[AGENT] port :${port} already bound (pids=${pids}) — probing HTTP"
  if http_any_alive "${port}"; then
    tier="$("${IG_AGENT_ROOT}/scripts/boot_acceptance.sh" --tier-only 2>/dev/null || echo red)"
    if [[ "${tier}" != "red" ]]; then
      log "[AGENT] reusing healthy agent on :${port} (boot tier=${tier})"
      export REUSE_EXISTING_AGENT=1
      return 0
    fi
    log "[AGENT] HTTP alive but boot tier=red — recycling listener"
  else
    log "[AGENT] port :${port} bound but HTTP unresponsive — zombie/orphan listener"
  fi

  kill_port_listeners "${port}"
  if ! wait_port_free "${port}" 30; then
    log "ERROR: port :${port} still bound after escalate (pids=$(pids_on_port "${port}"))"
    launcher_status_fail "Boot failed" "Port :${port} stuck — unresponsive agent (pids $(pids_on_port "${port}"))" 4
    exit 7
  fi
  unset REUSE_EXISTING_AGENT
  return 0
}

# Single-owner desktop launch — second double-click waits or exits cleanly.
acquire_launcher_lock() {
  local lockdir="${IG_AGENT_ROOT}/logs/.launcher.lock.d"
  mkdir -p "${IG_AGENT_ROOT}/logs"
  if [[ -f "${lockdir}/pid" ]]; then
    local holder
    holder="$(tr -d '[:space:]' < "${lockdir}/pid" 2>/dev/null || true)"
    if [[ -n "${holder}" ]] && ! kill -0 "${holder}" 2>/dev/null; then
      rm -rf "${lockdir}"
    fi
  fi
  if mkdir "${lockdir}" 2>/dev/null; then
    echo $$ > "${lockdir}/pid"
    trap 'rm -rf "${IG_AGENT_ROOT}/logs/.launcher.lock.d"' EXIT
    return 0
  fi
  if [[ "${LAUNCHER_WAIT_FOR_LOCK:-}" == "1" ]]; then
    local waited=0
    local max_wait="${LAUNCHER_LOCK_WAIT_SEC:-120}"
    log "launcher lock held — waiting up to ${max_wait}s"
    while (( waited < max_wait )); do
      if mkdir "${lockdir}" 2>/dev/null; then
        echo $$ > "${lockdir}/pid"
        trap 'rm -rf "${IG_AGENT_ROOT}/logs/.launcher.lock.d"' EXIT
        return 0
      fi
      sleep 1
      waited=$((waited + 1))
    done
  fi
  return 1
}

manual_stop_engaged() {
  "${IG_AGENT_PY}" -c "import sys; from system.shutdown_cleanup import manual_stop_active; sys.exit(0 if manual_stop_active() else 1)" \
    2>/dev/null
}

# Stop all daemon_supervisor processes — must run after mark_manual_stop.
stop_supervisor_stack() {
  local data_root
  data_root="${IG_DATA_ROOT:-$("${IG_AGENT_PY}" -c "import os; from runtime.app_mode import parse_app_mode, resolve_data_root; print(resolve_data_root(parse_app_mode(os.environ.get('APP_MODE','DEMO'))))" 2>/dev/null || echo "${IG_AGENT_ROOT}/src/data/v31-production")}"
  local pf="${data_root}/supervisor.pid"
  log "stop_supervisor_stack: engaging"
  if [[ -f "${pf}" ]]; then
    local spid
    spid="$(tr -d '[:space:]' < "${pf}" 2>/dev/null || true)"
    if [[ -n "${spid}" ]] && kill -0 "${spid}" 2>/dev/null; then
      log "stop_supervisor_stack: TERM supervisor pid=${spid}"
      kill -TERM "${spid}" 2>/dev/null || true
      sleep 2
      kill -0 "${spid}" 2>/dev/null && kill -9 "${spid}" 2>/dev/null || true
    fi
    rm -f "${pf}"
  fi
  kill_pattern "-TERM" "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh"
  sleep 2
  kill_pattern "-9" "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh"
  rm -f "${data_root}/port_hold.pid" "${data_root}/circuit_breaker.lock" 2>/dev/null || true
}

wait_for_supervisor_pid() {
  local data_root="${IG_DATA_ROOT:-}"
  if [[ -z "${data_root}" ]]; then
    data_root="$("${IG_AGENT_PY}" -c "import os; from runtime.app_mode import parse_app_mode, resolve_data_root; print(resolve_data_root(parse_app_mode(os.environ.get('APP_MODE','DEMO'))))" 2>/dev/null || echo "${IG_AGENT_ROOT}/src/data/v31-production")"
  fi
  local pf="${data_root}/supervisor.pid"
  local i pid
  for i in $(seq 1 30); do
    if [[ -f "${pf}" ]]; then
      pid="$(tr -d '[:space:]' < "${pf}" 2>/dev/null || true)"
      if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
        log "[AGENT] supervisor confirmed pid=${pid}"
        return 0
      fi
    fi
    sleep 1
  done
  log "WARN: supervisor.pid not live within 30s"
  return 1
}

verify_clean_slate() {
  local port="${PORT}"
  local blockers=()
  if [[ -n "$(pids_on_port "${port}")" ]]; then
    blockers+=("port_${port}_bound")
  fi
  if pgrep -f "${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh" >/dev/null 2>&1; then
    blockers+=("supervisor_running")
  fi
  if pgrep -f "${IG_AGENT_ROOT}/src/main.py" >/dev/null 2>&1; then
    blockers+=("main_py_running")
  fi
  if ((${#blockers[@]} > 0)); then
    log "verify_clean_slate: blockers=${blockers[*]}"
    return 1
  fi
  log "verify_clean_slate: ok"
  return 0
}
