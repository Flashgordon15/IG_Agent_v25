#!/usr/bin/env bash
# Install IG Agent v29 launchd jobs.
#
# Scheduled ops (always installed & bootstrapped):
#   - v29 daily digest 07:30 daily (operator briefing markdown)
#   - v29 roadmap morning 07:05 daily (snapshot + optional Telegram delta)
#   - v29 nightly 22:30 daily (Finnhub calendar + feature store)
#   - v29 synthetic replay 23:30 UTC (ML filter calibration via synthetic_replay.py)
#   - gate coherence 4×/day
#   - v29 weekly Sunday 08:30
#
# Flags:
#   (no flag)       — install supervision (caffeinate + watchdog) + scheduled ops
#                     Watchdog starts main.py; do NOT also load com.igagent.v25.plist
#                     (two starters fight and cause restart loops).
#   --with-agent-plist — also load com.igagent.v25.plist (advanced; usually avoid)
#   --weekly-only   — alias for --ops-only (legacy name)
#   --ops-only      — install scheduled ops only; skip caffeinate/watchdog
#                     Use when main.py is already running manually.
set -euo pipefail

WEEKLY_ONLY=0
WITH_AGENT_PLIST=0
case "${1:-}" in
  --weekly-only | --ops-only)
    WEEKLY_ONLY=1
    ;;
  --with-agent-plist)
    WITH_AGENT_PLIST=1
    ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
LAUNCH_DOMAIN="gui/$(id -u)"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"
WATCHDOG_PLIST="com.igagent.v25.watchdog.plist"
PROFIT_PLIST="com.igagent.v25.profitability.plist"
V29_WEEKLY_PLIST="com.igagent.v29weekly.plist"
GATE_COHERENCE_PLIST="com.igagent.v25.gatecoherence.plist"
V29_NIGHTLY_PLIST="com.igagent.v29nightly.plist"
V29_ROADMAP_PLIST="com.igagent.v29roadmap.plist"
V29_DIGEST_PLIST="com.igagent.v29digest.plist"
V29_REPLAY_PLIST="com.igagent.v29replay.plist"
LEGACY_V26_WEEKLY_PLIST="com.igagent.v25.v26weekly.plist"
LEGACY_V26_NIGHTLY_PLIST="com.igagent.v25.v26nightly.plist"
LOCK_FILE="${ROOT}/src/data/.ig_agent_v29.lock"

mkdir -p "${ROOT}/src/data/logs"
mkdir -p "${LAUNCH_AGENTS}"

PY=""
for candidate in \
  "${ROOT}/.venv/bin/python3" \
  "${ROOT}/venv/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
    PY="${candidate}"
    break
  fi
done
if [ -z "${PY}" ]; then
  echo "ERROR: no python3 executable found for launchd plists" >&2
  exit 1
fi

launchctl_label() {
  basename "$1" .plist
}

launchctl_bootout_job() {
  local plist_name="$1"
  local label
  label="$(launchctl_label "${plist_name}")"
  launchctl bootout "${LAUNCH_DOMAIN}/${label}" 2>/dev/null || true
}

launchctl_bootstrap_job() {
  local plist_name="$1"
  local label dest
  label="$(launchctl_label "${plist_name}")"
  dest="${LAUNCH_AGENTS}/${plist_name}"
  launchctl_bootout_job "${plist_name}"
  if launchctl bootstrap "${LAUNCH_DOMAIN}" "${dest}" 2>/dev/null; then
    echo "  loaded ${label}"
    return 0
  fi
  if launchctl print "${LAUNCH_DOMAIN}/${label}" >/dev/null 2>&1; then
    echo "  already loaded ${label}"
    return 0
  fi
  echo "ERROR: launchctl bootstrap failed for ${label}" >&2
  launchctl bootstrap "${LAUNCH_DOMAIN}" "${dest}" 2>&1 || true
  exit 1
}

stop_manual_agent_if_running() {
  clear_manual_stop_flag

  local pids
  pids="$(lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -z "${pids}" ]; then
    return 0
  fi

  if curl -sf --max-time 3 "http://127.0.0.1:8080/api/health" >/dev/null 2>&1; then
    echo "Agent healthy on :8080 — handoff to launchd without stopping (watchdog will supervise)"
    return 0
  fi

  echo "Stopping unhealthy manual agent on :8080 (pid ${pids}) so launchd can take over..."
  echo "${pids}" | xargs kill -TERM 2>/dev/null || true
  for _wait_i in $(seq 1 12); do
    pids="$(lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null || true)"
    if [ -z "${pids}" ]; then
      break
    fi
    sleep 2
  done
  pids="$(lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "${pids}" ]; then
    echo "${pids}" | xargs kill -KILL 2>/dev/null || true
    sleep 1
  fi
  rm -f "${LOCK_FILE}"
  if ! launchctl print "${LAUNCH_DOMAIN}/com.igagent.v25.watchdog" >/dev/null 2>&1; then
    pkill -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true
    sleep 1
  fi
}

clear_manual_stop_flag() {
  if [ -x "${PY}" ]; then
    IG_AGENT_ROOT="${ROOT}" PYTHONPATH="${ROOT}/src" "${PY}" -c "
from system.shutdown_cleanup import clear_manual_stop
clear_manual_stop()
" 2>/dev/null || rm -f "${ROOT}/src/data/state/manual_stop.json"
  else
    rm -f "${ROOT}/src/data/state/manual_stop.json"
  fi
}

install_plist() {
  local src_name="$1"
  sed -e "s|__IG_AGENT_ROOT__|${ROOT}|g" -e "s|__PYTHON_BIN__|${PY}|g" \
    "${ROOT}/scripts/${src_name}" > "${LAUNCH_AGENTS}/${src_name}"
}

echo "Installing plists (python=${PY})..."
install_plist "${AGENT_PLIST}"
install_plist "${CAFF_PLIST}"
install_plist "${WATCHDOG_PLIST}"
install_plist "${PROFIT_PLIST}"
install_plist "${V29_WEEKLY_PLIST}"
install_plist "${GATE_COHERENCE_PLIST}"
install_plist "${V29_NIGHTLY_PLIST}"
install_plist "${V29_ROADMAP_PLIST}"
install_plist "${V29_DIGEST_PLIST}"
install_plist "${V29_REPLAY_PLIST}"

echo "Unloading legacy v26 scheduled job labels (if present)..."
launchctl_bootout_job "${LEGACY_V26_WEEKLY_PLIST}"
launchctl_bootout_job "${LEGACY_V26_NIGHTLY_PLIST}"

echo "Loading scheduled ops jobs..."
launchctl_bootstrap_job "${V29_WEEKLY_PLIST}"
launchctl_bootstrap_job "${GATE_COHERENCE_PLIST}"
launchctl_bootstrap_job "${V29_NIGHTLY_PLIST}"
launchctl_bootstrap_job "${V29_ROADMAP_PLIST}"
launchctl_bootstrap_job "${V29_DIGEST_PLIST}"
launchctl_bootstrap_job "${V29_REPLAY_PLIST}"

if [ "${WEEKLY_ONLY}" -eq 0 ]; then
  stop_manual_agent_if_running
  echo "Loading agent supervision jobs (watchdog starts main.py)..."
  launchctl_bootstrap_job "${CAFF_PLIST}"
  launchctl_bootstrap_job "${WATCHDOG_PLIST}"
  launchctl_bootstrap_job "${PROFIT_PLIST}"
  if [ "${WITH_AGENT_PLIST}" -eq 1 ]; then
    echo "WARNING: --with-agent-plist also loads com.igagent.v25 (can fight watchdog)."
    launchctl_bootstrap_job "${AGENT_PLIST}"
  else
    launchctl_bootout_job "${AGENT_PLIST}"
    echo "Skipped com.igagent.v25.plist — watchdog owns agent start/restart."
  fi
  echo "Supervision installed: caffeinate + watchdog (+ profitability schedule)"
  if ! lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Nudging watchdog to start agent..."
    launchctl kickstart -k "${LAUNCH_DOMAIN}/com.igagent.v25.watchdog" 2>/dev/null || true
  fi
  for _wait_i in $(seq 1 36); do
    if curl -sf --max-time 2 "http://127.0.0.1:8080/api/health" >/dev/null 2>&1; then
      echo "Agent healthy on :8080"
      break
    fi
    if [ "${_wait_i}" -eq 36 ]; then
      echo "WARN: agent not healthy after 3 min — check watchdog.log and agent_restart.log" >&2
    fi
    sleep 5
  done
else
  echo "Ops-only mode: skipped ${AGENT_PLIST} bootstrap (manual agent left running)"
  echo "  Nightly/gate-coherence/weekly jobs were still installed and bootstrapped."
fi

echo ""
echo "Installed plists to ${LAUNCH_AGENTS}"
echo "v29 weekly: Sunday 08:30 → scripts/v26_weekly_pack.py (log: src/data/logs/v29_weekly.log)"
echo "gate coherence: 00:30/06:30/12:30/18:30 → scripts/run_gate_coherence_check.py (log: src/data/logs/gate_coherence.log)"
echo "v29 daily digest: 07:30 → scripts/daily_operator_digest.py (log: src/data/logs/v29_daily_digest.log)"
echo "v29 roadmap morning: 07:05 → scripts/roadmap_morning_report.py (snapshot; log: src/data/logs/v29_roadmap_morning.log)"
echo "v29 nightly: 22:30 → scripts/v26_nightly.py (Finnhub calendar + feature store; log: src/data/logs/v29_nightly.log)"
echo "v29 replay: 23:30 UTC → src/system/synthetic_replay.py (ML filter calibration; log: src/data/logs/synthetic_replay_launchd.log)"
echo "Logs: ${ROOT}/src/data/logs/launchd_stdout.log"
echo ""
echo "Verify overnight supervision:"
echo "  ./scripts/ensure_overnight_ready.sh"
