#!/usr/bin/env bash
# Unload and remove IG Agent v25 launchd jobs.
set -euo pipefail

LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"

launchctl bootout "gui/$(id -u)/${AGENT_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${CAFF_PLIST}" 2>/dev/null || true

rm -f "${LAUNCH_AGENTS}/${AGENT_PLIST}" "${LAUNCH_AGENTS}/${CAFF_PLIST}"

echo "Unloaded and removed ${AGENT_PLIST} and ${CAFF_PLIST}"
