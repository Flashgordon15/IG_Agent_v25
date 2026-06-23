#!/usr/bin/env bash
# Install launchd job to keep desktop cockpit alive (self-healing GUI).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST_SRC="${ROOT}/scripts/com.igagent.v30.cockpit.plist"
PLIST_DST="${LAUNCH_AGENTS}/com.igagent.v30.cockpit.plist"
PYTHON_BIN="${ROOT}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

mkdir -p "${LAUNCH_AGENTS}" "${ROOT}/src/data/logs"
sed \
  -e "s|__IG_AGENT_ROOT__|${ROOT}|g" \
  -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
  "${PLIST_SRC}" > "${PLIST_DST}"

UID_DOMAIN="gui/$(id -u)"
launchctl bootout "${UID_DOMAIN}" "${PLIST_DST}" 2>/dev/null || true
launchctl bootstrap "${UID_DOMAIN}" "${PLIST_DST}"
launchctl enable "${UID_DOMAIN}/com.igagent.v30.cockpit"
echo "Installed com.igagent.v30.cockpit — logs: ${ROOT}/src/data/logs/cockpit_launchd.log"
