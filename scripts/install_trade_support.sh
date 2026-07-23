#!/usr/bin/env bash
# Install Trade Support Wrapper launchd job (always-on open-trade supervisor).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST="com.igagent.trade_support.plist"
LABEL="com.igagent.trade_support"
LOG_DIR="${ROOT}/src/data/logs"
WRAPPER="${ROOT}/scripts/trade_support_wrapper.sh"

mkdir -p "${LOG_DIR}" "${LAUNCH_AGENTS}"
chmod +x "${WRAPPER}" 2>/dev/null || true

cat > "${LAUNCH_AGENTS}/${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${WRAPPER}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>APP_MODE</key>
    <string>DEMO</string>
    <key>IG_AGENT_CONFIG</key>
    <string>config/config_v31_demo_throughput.json</string>
    <key>PYTHONPATH</key>
    <string>${ROOT}/src</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/trade_support_wrapper.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/trade_support_wrapper.err.log</string>
  <key>ThrottleInterval</key>
  <integer>15</integer>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${PLIST}"
launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true

echo "Installed ${PLIST} — logs: ${LOG_DIR}/trade_support_wrapper.log"
