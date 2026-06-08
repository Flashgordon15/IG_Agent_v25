#!/usr/bin/env bash
# Install IG Agent v25 launchd jobs.
#
# Scheduled ops (always installed & bootstrapped):
#   - v26 nightly 22:30 daily (Finnhub calendar + feature store)
#   - gate coherence 4×/day
#   - v26 weekly Sunday 08:30
#
# Flags:
#   (no flag)       — install everything including main agent (starts main.py)
#   --weekly-only   — alias for --ops-only (legacy name)
#   --ops-only      — install scheduled ops only; skip main agent/caffeinate/watchdog
#                     Use when main.py is already running manually.
set -euo pipefail

WEEKLY_ONLY=0
case "${1:-}" in
  --weekly-only | --ops-only)
    WEEKLY_ONLY=1
    ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"
WATCHDOG_PLIST="com.igagent.v25.watchdog.plist"
PROFIT_PLIST="com.igagent.v25.profitability.plist"
V26_WEEKLY_PLIST="com.igagent.v25.v26weekly.plist"
GATE_COHERENCE_PLIST="com.igagent.v25.gatecoherence.plist"
V26_NIGHTLY_PLIST="com.igagent.v25.v26nightly.plist"

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

install_plist() {
  local src_name="$1"
  sed -e "s|__IG_AGENT_ROOT__|${ROOT}|g" -e "s|__PYTHON_BIN__|${PY}|g" \
    "${ROOT}/scripts/${src_name}" > "${LAUNCH_AGENTS}/${src_name}"
}

install_plist "${AGENT_PLIST}"
install_plist "${CAFF_PLIST}"
install_plist "${WATCHDOG_PLIST}"
install_plist "${PROFIT_PLIST}"
install_plist "${V26_WEEKLY_PLIST}"
install_plist "${GATE_COHERENCE_PLIST}"
install_plist "${V26_NIGHTLY_PLIST}"

launchctl bootout "gui/$(id -u)/${V26_WEEKLY_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${V26_WEEKLY_PLIST}"
launchctl bootout "gui/$(id -u)/${GATE_COHERENCE_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${GATE_COHERENCE_PLIST}"
launchctl bootout "gui/$(id -u)/${V26_NIGHTLY_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${V26_NIGHTLY_PLIST}"

if [ "${WEEKLY_ONLY}" -eq 0 ]; then
  launchctl bootout "gui/$(id -u)/${AGENT_PLIST}" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/${CAFF_PLIST}" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/${WATCHDOG_PLIST}" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/${PROFIT_PLIST}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${AGENT_PLIST}"
  launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${CAFF_PLIST}"
  launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${WATCHDOG_PLIST}"
  launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${PROFIT_PLIST}"
  echo "Installed all jobs including ${AGENT_PLIST} (RunAtLoad — will start main.py if not running)"
else
  echo "Ops-only mode: skipped ${AGENT_PLIST} bootstrap (manual agent left running)"
  echo "  Nightly/gate-coherence/weekly jobs were still installed and bootstrapped."
fi

echo "Installed plists to ${LAUNCH_AGENTS}"
echo "v26 weekly: Sunday 08:30 → scripts/v26_weekly_pack.py (log: src/data/logs/v26_weekly.log)"
echo "gate coherence: 00:30/06:30/12:30/18:30 → scripts/run_gate_coherence_check.py (log: src/data/logs/gate_coherence.log)"
echo "v26 nightly: 22:30 → scripts/v26_nightly.py (Finnhub calendar + feature store; log: src/data/logs/v26_nightly.log)"
echo "Logs: ${ROOT}/src/data/logs/launchd_stdout.log"
