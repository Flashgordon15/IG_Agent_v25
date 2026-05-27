#!/usr/bin/env bash
# Install IG Agent v25 launchd jobs (agent + caffeinate).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"

mkdir -p "${ROOT}/src/data/logs"
mkdir -p "${LAUNCH_AGENTS}"

cp "${ROOT}/scripts/${AGENT_PLIST}" "${LAUNCH_AGENTS}/${AGENT_PLIST}"
cp "${ROOT}/scripts/${CAFF_PLIST}" "${LAUNCH_AGENTS}/${CAFF_PLIST}"

launchctl bootout "gui/$(id -u)/${AGENT_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${CAFF_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${AGENT_PLIST}"
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${CAFF_PLIST}"

echo "Installed ${AGENT_PLIST} and ${CAFF_PLIST} to ${LAUNCH_AGENTS}"
echo "Logs: ${ROOT}/src/data/logs/launchd_stdout.log"
