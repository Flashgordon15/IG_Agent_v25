#!/usr/bin/env bash
# Unload and remove IG Agent v25 launchd jobs.
set -euo pipefail

LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"
WATCHDOG_PLIST="com.igagent.v25.watchdog.plist"
PROFIT_PLIST="com.igagent.v25.profitability.plist"

launchctl bootout "gui/$(id -u)/${AGENT_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${CAFF_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${WATCHDOG_PLIST}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${PROFIT_PLIST}" 2>/dev/null || true

rm -f "${LAUNCH_AGENTS}/${AGENT_PLIST}" "${LAUNCH_AGENTS}/${CAFF_PLIST}" \
  "${LAUNCH_AGENTS}/${WATCHDOG_PLIST}" "${LAUNCH_AGENTS}/${PROFIT_PLIST}"

echo "Unloaded and removed ${AGENT_PLIST}, ${CAFF_PLIST}, ${WATCHDOG_PLIST}, and ${PROFIT_PLIST}"
