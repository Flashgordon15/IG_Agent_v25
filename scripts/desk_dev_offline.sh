#!/usr/bin/env bash
# Take the agent OFFLINE for development while opens may exist.
#
# - Anti-zombie stop of main.py (never kill -9)
# - Leaves trade_support UP (supervises opens)
# - Arms manual_stop + entry pause + offline_for_dev
#
# Usage:
#   ./scripts/desk_dev_offline.sh          # stop main, keep supervisors
#   ./scripts/desk_dev_offline.sh status
#
# Reload later (opens OK):
#   ./scripts/desk_deploy.sh deploy --force-open-book
#   # or: ./scripts/desk_deploy.sh deploy --dev
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
# shellcheck source=lib/detach_exec.sh
source "${ROOT}/scripts/lib/detach_exec.sh"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
PY="${ROOT}/.venv/bin/python3"
if [ ! -x "${PY}" ]; then
  PY="$(command -v python3)"
fi

log() { printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

CMD="${1:-offline}"

_ensure_trade_support() {
  local label="gui/$(id -u)/com.igagent.trade_support"
  if pgrep -f "runtime.trade_support_wrapper" >/dev/null 2>&1; then
    log "trade_support already alive pid=$(pgrep -f 'runtime.trade_support_wrapper' | head -1)"
    return 0
  fi
  if launchctl print "${label}" >/dev/null 2>&1; then
    log "kickstarting trade_support via launchctl"
    launchctl kickstart -k "${label}" 2>/dev/null || true
    sleep 2
  fi
  if ! pgrep -f "runtime.trade_support_wrapper" >/dev/null 2>&1; then
    log "spawning trade_support_wrapper.sh"
    mkdir -p "${ROOT}/src/data/logs"
    detach_exec --log "${ROOT}/src/data/logs/trade_support_wrapper.log" -- \
      "${ROOT}/scripts/trade_support_wrapper.sh"
    sleep 2
  fi
  if pgrep -f "runtime.trade_support_wrapper" >/dev/null 2>&1; then
    log "trade_support armed pid=$(pgrep -f 'runtime.trade_support_wrapper' | head -1)"
    return 0
  fi
  log "WARN trade_support not running — opens may be unsupervised"
  return 1
}

_stop_main_keep_supervisors() {
  log "offline: pause entries + offline_for_dev (no flatten)"
  "${PY}" -c "
from runtime.desk_dev_controls import mark_offline_for_dev
import json
print(json.dumps(mark_offline_for_dev(reason='desk_dev_offline'), indent=2, default=str))
"

  log "offline: mark_manual_stop (freeze watchdog)"
  "${PY}" -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='desk_dev_offline')"

  # Ensure supervisors BEFORE killing main so opens never go bare.
  _ensure_trade_support || true

  local pid
  pid="$(pgrep -f 'src/main.py' | head -1 || true)"
  if [ -z "${pid}" ]; then
    log "offline: main already down"
  else
    log "offline: SIGTERM main.py pid=${pid} (never kill -9)"
    kill -TERM "${pid}" 2>/dev/null || true
    local i=0
    while ps -p "${pid}" >/dev/null 2>&1; do
      i=$((i + 1))
      if [ "${i}" -eq 20 ] && ! lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
        kill -TERM "${pid}" 2>/dev/null || true
      fi
      if [ "${i}" -eq 30 ] && ps -p "${pid}" >/dev/null 2>&1; then
        kill -INT "${pid}" 2>/dev/null || true
      fi
      if [ "${i}" -ge 45 ]; then
        log "ERROR: main pid=${pid} still alive after 45s — refusing kill -9"
        return 1
      fi
      sleep 1
    done
    log "offline: main exited"
  fi

  # Port free (best effort)
  local j=0
  while lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; do
    j=$((j + 1))
    [ "${j}" -ge 15 ] && break
    sleep 1
  done

  # Evict bytecode so next start loads new code — safe while main is down.
  find src -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find src -name '*.pyc' -delete 2>/dev/null || true
  rm -f src/data/.ig_agent_v29.lock src/data/.ig_agent_v31.lock 2>/dev/null || true

  _ensure_trade_support || true
  log "OFFLINE: main down; trade_support supervising; entries paused"
  log "Reload: ./scripts/desk_deploy.sh deploy --force-open-book   # or --dev"
}

case "${CMD}" in
  offline|stop|down)
    _stop_main_keep_supervisors
    ;;
  status)
    "${PY}" -c "
from runtime.desk_dev_controls import status_snapshot
import json, subprocess
snap = status_snapshot()
snap['main_pids'] = subprocess.getoutput(\"pgrep -f 'src/main.py' || true\").split()
snap['trade_support_pids'] = subprocess.getoutput(\"pgrep -f 'runtime.trade_support_wrapper' || true\").split()
print(json.dumps(snap, indent=2, default=str))
"
    ;;
  -h|--help|help)
    sed -n '2,16p' "$0"
    ;;
  *)
    echo "Unknown command: ${CMD} (offline|status)" >&2
    exit 1
    ;;
esac
