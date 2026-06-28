#!/usr/bin/env bash
# IG Agent v31.1.0 — background supervisor daemon.
# Encapsulates main.py with health polling, SIGTERM recovery, and 3-strike circuit breaker.
#
# Usage:
#   ./scripts/daemon_supervisor.sh          # foreground (launchd / nohup)
#   nohup ./scripts/daemon_supervisor.sh >> src/data/v31-production/logs/supervisor.log 2>&1 &
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

V31_DATA="${IG_DATA_ROOT:-${AGENT_ROOT}/src/data/v31-production}"
LOG_DIR="${V31_DATA}/logs"
APP_MODE="${APP_MODE:-DEMO}"
IG_ACCOUNT_SCOPE="${IG_ACCOUNT_SCOPE:-}"
IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31.json}"
IG_BROKER_PLANE="${IG_BROKER_PLANE:-DEMO}"
SUPERVISOR_LOG="${LOG_DIR}/supervisor.log"

# Self-daemonize when launched interactively (survives parent shell exit).
if [[ "${DAEMON_SUPERVISOR_REDIRECT:-}" != "1" ]]; then
  mkdir -p "${LOG_DIR}"
  export DAEMON_SUPERVISOR_REDIRECT=1
  nohup "${BASH_SOURCE[0]}" "$@" >> "${SUPERVISOR_LOG}" 2>&1 &
  disown 2>/dev/null || true
  echo "daemon_supervisor detached pid=$! log=${SUPERVISOR_LOG}"
  exit 0
fi

cd "${AGENT_ROOT}"
SUPERVISOR_PID_FILE="${V31_DATA}/supervisor.pid"
AGENT_PID_FILE="${V31_DATA}/agent.pid"
CIRCUIT_LOCK="${V31_DATA}/circuit_breaker.lock"
CRASH_HISTORY="${V31_DATA}/crash_history.json"
PORT_HOLD_PID_FILE="${V31_DATA}/port_hold.pid"

API_PORT="${IG_API_PORT:-8080}"
HEALTH_URL="http://127.0.0.1:${API_PORT}/api/health"
POLL_INTERVAL_SEC=10
UNHEALTHY_503_SEC=60
MAX_CRASHES=3
CRASH_WINDOW_SEC=600
BOOT_GRACE_SEC=180

PY="${AGENT_ROOT}/.venv/bin/python3"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3 || true)"
fi

mkdir -p "${LOG_DIR}"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${SUPERVISOR_LOG}"
}

resolve_python() {
  if [[ -x "${AGENT_ROOT}/.venv/bin/python3" ]]; then
    printf '%s\n' "${AGENT_ROOT}/.venv/bin/python3"
    return 0
  fi
  command -v python3 2>/dev/null || printf '%s\n' "python3"
}

send_telegram_alert() {
  local message="$1"
  local py
  py="$(resolve_python)"
  TELEGRAM_MSG="${message}" PYTHONPATH="${AGENT_ROOT}/src" "${py}" -c "
import os
try:
    from system.telegram_notifier import send_critical_alert
    send_critical_alert(
        os.environ.get('TELEGRAM_MSG', ''),
        dedupe_key='daemon_supervisor:circuit_breaker',
    )
except Exception as exc:
    print(f'telegram alert failed: {exc}')
" 2>/dev/null || true
}

supervisor_already_running() {
  if [[ ! -f "${SUPERVISOR_PID_FILE}" ]]; then
    return 1
  fi
  local old_pid
  old_pid="$(tr -d '[:space:]' < "${SUPERVISOR_PID_FILE}" 2>/dev/null || true)"
  [[ -n "${old_pid}" && "${old_pid}" != "$$" ]] && kill -0 "${old_pid}" 2>/dev/null
}

circuit_breaker_active() {
  [[ -f "${CIRCUIT_LOCK}" ]]
}

open_circuit_breaker() {
  local reason="$1"
  : > "${CIRCUIT_LOCK}"
  date -u '+%Y-%m-%dT%H:%M:%SZ' >> "${CIRCUIT_LOCK}"
  printf '\n%s\n' "${reason}" >> "${CIRCUIT_LOCK}"
  log "CIRCUIT BREAKER OPEN — ${reason}"
  hold_port_8080
  send_telegram_alert "🚨 IG Agent v31.1.0 SUPERVISOR CIRCUIT BREAKER

${reason}

Port :${API_PORT} locked. Manual intervention required.
Clear: rm -f ${CIRCUIT_LOCK} and restart supervisor."
}

hold_port_8080() {
  if [[ -f "${PORT_HOLD_PID_FILE}" ]]; then
    local hold_pid
    hold_pid="$(tr -d '[:space:]' < "${PORT_HOLD_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${hold_pid}" ]] && kill -0 "${hold_pid}" 2>/dev/null; then
      return 0
    fi
  fi
  local py
  py="$(resolve_python)"
  nohup "${py}" -u - <<PY >> "${SUPERVISOR_LOG}" 2>&1 &
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", ${API_PORT}))
s.listen(1)
print("port_hold: bound 127.0.0.1:${API_PORT}", flush=True)
while True:
    time.sleep(3600)
PY
  echo $! > "${PORT_HOLD_PID_FILE}"
  log "port_hold: :${API_PORT} locked pid=$(cat "${PORT_HOLD_PID_FILE}")"
}

release_port_hold() {
  if [[ -f "${PORT_HOLD_PID_FILE}" ]]; then
    local hold_pid
    hold_pid="$(tr -d '[:space:]' < "${PORT_HOLD_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${hold_pid}" ]]; then
      kill -TERM "${hold_pid}" 2>/dev/null || true
    fi
    rm -f "${PORT_HOLD_PID_FILE}"
  fi
}

wait_port_free() {
  local deadline=$(( $(date +%s) + 30 ))
  while lsof -iTCP:"${API_PORT}" -sTCP:LISTEN -t >/dev/null 2>&1; do
    if (( $(date +%s) >= deadline )); then
      log "eviction: port :${API_PORT} still bound after 30s"
      return 1
    fi
    sleep 1
  done
  return 0
}

evict_stale_processes() {
  local mode="${1:-full}"
  log "eviction: clearing stale ig_agent / :${API_PORT} occupants (mode=${mode})"
  if [[ -x "${PY}" ]] || command -v python3 >/dev/null 2>&1; then
    PYTHONPATH="${AGENT_ROOT}/src" "$(resolve_python)" -c \
      "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='daemon_supervisor_boot')" \
      2>/dev/null || true
  fi

  # Graceful SIGTERM for project main.py first (anti-zombie protocol).
  if pgrep -f "${AGENT_ROOT}/src/main.py" >/dev/null 2>&1; then
    pgrep -f "${AGENT_ROOT}/src/main.py" | xargs kill -TERM 2>/dev/null || true
    wait_port_free || true
  fi

  # Native process eviction per v31.1.0 deployment spec (cold boot only).
  if [[ "${mode}" == "full" ]]; then
    pkill -f ig_agent 2>/dev/null || true
    sleep 1
  fi

  if lsof -iTCP:"${API_PORT}" -sTCP:LISTEN -t >/dev/null 2>&1; then
    lsof -tiTCP:"${API_PORT}" -sTCP:LISTEN 2>/dev/null | xargs kill -TERM 2>/dev/null || true
    sleep 2
  fi

  wait_port_free || true

  find "${AGENT_ROOT}/src" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "${AGENT_ROOT}/src" -name '*.pyc' -delete 2>/dev/null || true
  rm -f "${AGENT_ROOT}/src/data/.ig_agent_v29.lock" \
        "${AGENT_ROOT}/src/data/.ig_agent_v30_port_${API_PORT}.lock" 2>/dev/null || true

  PYTHONPATH="${AGENT_ROOT}/src" "$(resolve_python)" -c \
    "from system.shutdown_cleanup import clear_manual_stop; clear_manual_stop()" \
    2>/dev/null || true

  log "eviction: complete — port :${API_PORT} clear=$(lsof -iTCP:"${API_PORT}" -sTCP:LISTEN -t >/dev/null 2>&1 && echo no || echo yes)"
}

record_crash() {
  local reason="$1"
  local py ts
  py="$(resolve_python)"
  ts="$(date +%s)"
  CRASH_REASON="${reason}" CRASH_TS="${ts}" CRASH_HISTORY_PATH="${CRASH_HISTORY}" \
    PYTHONPATH="${AGENT_ROOT}/src" "${py}" -c "
import json, os, time
from pathlib import Path
path = Path(os.environ['CRASH_HISTORY_PATH'])
ts = float(os.environ['CRASH_TS'])
reason = os.environ['CRASH_REASON']
window = ${CRASH_WINDOW_SEC}
try:
    data = json.loads(path.read_text()) if path.is_file() else {'events': []}
except Exception:
    data = {'events': []}
events = [e for e in data.get('events', []) if ts - float(e.get('ts', 0)) <= window]
events.append({'ts': ts, 'reason': reason})
data['events'] = events
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2), encoding='utf-8')
print(len(events))
" 2>/dev/null || true
}

crash_count_in_window() {
  local py count
  py="$(resolve_python)"
  count="$(PYTHONPATH="${AGENT_ROOT}/src" "${py}" - <<PY "${CRASH_HISTORY}" 2>/dev/null || echo 0
import json, sys, time
from pathlib import Path
path = Path(sys.argv[1])
now = time.time()
window = ${CRASH_WINDOW_SEC}
try:
    data = json.loads(path.read_text()) if path.is_file() else {"events": []}
except Exception:
    data = {"events": []}
events = [e for e in data.get("events", []) if now - float(e.get("ts", 0)) <= window]
print(len(events))
PY
)"
  printf '%s\n' "${count:-0}"
}

stop_agent_graceful() {
  local pid=""
  if [[ -f "${AGENT_PID_FILE}" ]]; then
    pid="$(tr -d '[:space:]' < "${AGENT_PID_FILE}" 2>/dev/null || true)"
  fi
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    pid="$(pgrep -f "${AGENT_ROOT}/src/main.py" 2>/dev/null | head -1 || true)"
  fi
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    log "recovery: SIGTERM agent pid=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
    local deadline=$(( $(date +%s) + 30 ))
    while kill -0 "${pid}" 2>/dev/null; do
      if (( $(date +%s) >= deadline )); then
        log "recovery: agent pid=${pid} did not exit — sending SIGKILL"
        kill -KILL "${pid}" 2>/dev/null || true
        break
      fi
      sleep 1
    done
  fi
  rm -f "${AGENT_PID_FILE}"
  wait_port_free || true
}

start_agent_inner() {
  export APP_MODE="${APP_MODE}"
  export IG_ACCOUNT_SCOPE="${IG_ACCOUNT_SCOPE}"
  export IG_BROKER_PLANE="${IG_BROKER_PLANE}"
  export IG_DATA_ROOT="${V31_DATA}"
  export IG_API_PORT="${API_PORT}"
  export IG_SHARE_ENGINE=1
  export IG_AGENT_CONFIG="${IG_AGENT_CONFIG}"
  export PYTHONPATH="${AGENT_ROOT}/src"
  export IG_AGENT_ROOT="${AGENT_ROOT}"

  log "launch: starting v31.1.0 core (APP_MODE=${APP_MODE} config=${IG_AGENT_CONFIG} :${API_PORT} scope=${IG_ACCOUNT_SCOPE:-masked})"
  cd "${AGENT_ROOT}"
  nohup "${PY}" -u src/main.py >> "${LOG_DIR}/agent_stdout.log" 2>&1 &
  local agent_pid=$!
  echo "${agent_pid}" > "${AGENT_PID_FILE}"
  AGENT_START_EPOCH=$(date +%s)
  log "launch: agent detached pid=${agent_pid}"
}

start_agent() {
  release_port_hold
  evict_stale_processes full
  start_agent_inner
}

agent_process_alive() {
  local pid=""
  if [[ -f "${AGENT_PID_FILE}" ]]; then
    pid="$(tr -d '[:space:]' < "${AGENT_PID_FILE}" 2>/dev/null || true)"
  fi
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  if pgrep -f "${AGENT_ROOT}/src/main.py" >/dev/null 2>&1; then
    local live_pid
    live_pid="$(pgrep -f "${AGENT_ROOT}/src/main.py" 2>/dev/null | head -1 || true)"
    if [[ -n "${live_pid}" ]]; then
      echo "${live_pid}" > "${AGENT_PID_FILE}"
    fi
    return 0
  fi
  if lsof -iTCP:"${API_PORT}" -sTCP:LISTEN -t >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

agent_in_boot_grace() {
  local now_epoch
  now_epoch="$(date +%s)"
  # Grace covers full cold boot before :8080 bind (Gate1/Gate2 hydration).
  if (( AGENT_START_EPOCH > 0 && now_epoch - AGENT_START_EPOCH < BOOT_GRACE_SEC )); then
    return 0
  fi
  return 1
}

fetch_health_code() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -H 'User-Agent: IG-Agent-Supervisor/31.1.0' \
    "${HEALTH_URL}" 2>/dev/null || true)"
  code="$(printf '%s' "${code}" | tr -cd '0-9')"
  if [[ -z "${code}" ]]; then
    printf '000'
  else
    printf '%s' "${code}"
  fi
}

recovery_restart() {
  local reason="$1"
  if circuit_breaker_active; then
    log "recovery suppressed — circuit breaker active"
    return 1
  fi
  record_crash "${reason}" >/dev/null
  local crashes
  crashes="$(crash_count_in_window)"
  log "recovery: reason=${reason} crashes_in_window=${crashes}/${MAX_CRASHES}"
  if (( crashes >= MAX_CRASHES )); then
    open_circuit_breaker "3 crashes within ${CRASH_WINDOW_SEC}s — last: ${reason}"
    return 1
  fi
  stop_agent_graceful
  sleep 2
  evict_stale_processes soft
  start_agent_inner
  UNHEALTHY_503_SINCE=0
  return 0
}

trap 'log "supervisor: SIGTERM — shutting down"; rm -f "${SUPERVISOR_PID_FILE}"; exit 0' TERM
trap 'log "supervisor: SIGINT — shutting down"; rm -f "${SUPERVISOR_PID_FILE}"; exit 0' INT
trap 'rc=$?; if (( rc != 0 )); then log "supervisor: exiting rc=${rc}"; fi; rm -f "${SUPERVISOR_PID_FILE}"' EXIT

if supervisor_already_running; then
  echo "daemon_supervisor already running pid=$(cat "${SUPERVISOR_PID_FILE}")" >&2
  exit 0
fi

echo "$$" > "${SUPERVISOR_PID_FILE}"
log "=== IG Agent v31.1.0 supervisor online pid=$$ root=${AGENT_ROOT} ==="

if circuit_breaker_active; then
  log "supervisor: circuit breaker lock present — holding :${API_PORT} only"
  hold_port_8080
  while true; do
    sleep "${POLL_INTERVAL_SEC}"
  done
fi

agent_is_operational() {
  local code
  code="$(fetch_health_code)"
  if [[ "${code}" == "200" || "${code}" == "503" ]]; then
    return 0
  fi
  if lsof -iTCP:"${API_PORT}" -sTCP:LISTEN -t >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

AGENT_START_EPOCH=0
UNHEALTHY_503_SINCE=0
POLL_COUNT=0

if agent_is_operational; then
  log "supervisor: existing agent detected — adopting monitor (no cold eviction)"
  pgrep -f "${AGENT_ROOT}/src/main.py" 2>/dev/null | head -1 > "${AGENT_PID_FILE}" || true
  # Adopted sessions are already past boot — do not reset grace (avoids false deferrals).
  AGENT_START_EPOCH=0
else
  start_agent
fi

while true; do
  if circuit_breaker_active; then
    hold_port_8080
    sleep "${POLL_INTERVAL_SEC}"
    continue
  fi

  if ! agent_process_alive; then
    if agent_in_boot_grace; then
      log "health: boot grace — :${API_PORT} bound, deferring recovery"
      sleep "${POLL_INTERVAL_SEC}"
      continue
    fi
    log "health: agent process not running"
    recovery_restart "agent process died" || break
    sleep "${POLL_INTERVAL_SEC}"
    continue
  fi

  code="$(fetch_health_code)"
  now_epoch="$(date +%s)"

  if [[ "${code}" == "503" ]]; then
    if (( UNHEALTHY_503_SINCE == 0 )); then
      UNHEALTHY_503_SINCE=${now_epoch}
      log "health: HTTP 503 — starting unhealthy timer"
    else
      unhealthy_for=$(( now_epoch - UNHEALTHY_503_SINCE ))
      log "health: HTTP 503 for ${unhealthy_for}s (threshold=${UNHEALTHY_503_SEC}s)"
      if (( unhealthy_for >= UNHEALTHY_503_SEC )); then
        recovery_restart "HTTP 503 for >=${UNHEALTHY_503_SEC}s" || break
      fi
    fi
  elif [[ "${code}" == "200" ]]; then
    if (( UNHEALTHY_503_SINCE != 0 )); then
      log "health: HTTP 200 — operational (cleared 503 timer)"
    fi
    UNHEALTHY_503_SINCE=0
    POLL_COUNT=$((POLL_COUNT + 1))
    if (( POLL_COUNT % 6 == 0 )); then
      log "health: HTTP 200 — monitoring active (poll=${POLL_COUNT})"
    fi
  else
    log "health: HTTP ${code} (non-200/503)"
    if [[ "${code}" == "000" ]]; then
      if agent_in_boot_grace || agent_process_alive; then
        log "health: agent booting — deferring recovery on unreachable /api/health"
        sleep "${POLL_INTERVAL_SEC}"
        continue
      fi
      if (( UNHEALTHY_503_SINCE == 0 )); then
        UNHEALTHY_503_SINCE=${now_epoch}
      elif (( now_epoch - UNHEALTHY_503_SINCE >= UNHEALTHY_503_SEC )); then
        recovery_restart "health endpoint unreachable >=${UNHEALTHY_503_SEC}s" || break
      fi
    fi
  fi

  sleep "${POLL_INTERVAL_SEC}"
done

log "supervisor: exiting main loop"
rm -f "${SUPERVISOR_PID_FILE}"
