#!/usr/bin/env bash
# Unload and remove IG Agent v29 launchd jobs.
set -euo pipefail

LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
LAUNCH_DOMAIN="gui/$(id -u)"
AGENT_PLIST="com.igagent.v25.plist"
CAFF_PLIST="com.igagent.v25.caffeinate.plist"
WATCHDOG_PLIST="com.igagent.v25.watchdog.plist"
PROFIT_PLIST="com.igagent.v25.profitability.plist"
GATE_COHERENCE_PLIST="com.igagent.v25.gatecoherence.plist"
V29_NIGHTLY_PLIST="com.igagent.v29nightly.plist"
V29_WEEKLY_PLIST="com.igagent.v29weekly.plist"
LEGACY_V26_NIGHTLY_PLIST="com.igagent.v25.v26nightly.plist"
LEGACY_V26_WEEKLY_PLIST="com.igagent.v25.v26weekly.plist"

for job in \
  "${AGENT_PLIST%.plist}" \
  "${CAFF_PLIST%.plist}" \
  "${WATCHDOG_PLIST%.plist}" \
  "${PROFIT_PLIST%.plist}" \
  "${GATE_COHERENCE_PLIST%.plist}" \
  "${V29_NIGHTLY_PLIST%.plist}" \
  "${V29_WEEKLY_PLIST%.plist}" \
  "${LEGACY_V26_NIGHTLY_PLIST%.plist}" \
  "${LEGACY_V26_WEEKLY_PLIST%.plist}"
do
  launchctl bootout "${LAUNCH_DOMAIN}/${job}" 2>/dev/null || true
done

rm -f \
  "${LAUNCH_AGENTS}/${AGENT_PLIST}" \
  "${LAUNCH_AGENTS}/${CAFF_PLIST}" \
  "${LAUNCH_AGENTS}/${WATCHDOG_PLIST}" \
  "${LAUNCH_AGENTS}/${PROFIT_PLIST}" \
  "${LAUNCH_AGENTS}/${GATE_COHERENCE_PLIST}" \
  "${LAUNCH_AGENTS}/${V29_NIGHTLY_PLIST}" \
  "${LAUNCH_AGENTS}/${V29_WEEKLY_PLIST}" \
  "${LAUNCH_AGENTS}/${LEGACY_V26_NIGHTLY_PLIST}" \
  "${LAUNCH_AGENTS}/${LEGACY_V26_WEEKLY_PLIST}"

echo "Unloaded and removed IG Agent launchd plists (v29 scheduled ops + supervision)"
