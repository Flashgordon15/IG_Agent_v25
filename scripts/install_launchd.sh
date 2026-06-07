#!/usr/bin/env bash
# Install IG Agent v25 launchd jobs (agent + caffeinate).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"
WATCHDOG_PLIST="com.igagent.v25.watchdog.plist"
PROFIT_PLIST="com.igagent.v25.profitability.plist"

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

launchctl bootout "gui/$(id -u)/${AGENT_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${CAFF_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${WATCHDOG_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${PROFIT_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${AGENT_PLIST}"
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${CAFF_PLIST}"
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${WATCHDOG_PLIST}"
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${PROFIT_PLIST}"

echo "Installed ${AGENT_PLIST}, ${CAFF_PLIST}, ${WATCHDOG_PLIST}, and ${PROFIT_PLIST} to ${LAUNCH_AGENTS}"
echo "Watchdog keeper: KeepAlive=true — relaunches watchdog after restart-storm exit"
echo "Logs: ${ROOT}/src/data/logs/launchd_stdout.log"
