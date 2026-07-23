#!/usr/bin/env bash
# Install Desk Support Wrapper launchd job (24/7 out-of-process monitoring).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST="com.igagent.desk_support.plist"
LABEL="com.igagent.desk_support"
LOG_DIR="${ROOT}/src/data/logs"
WRAPPER="${ROOT}/scripts/desk_support_wrapper.sh"

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
    <key>IG_DATA_ROOT</key>
    <string>${ROOT}/src/data/v31-production</string>
    <key>IG_AGENT_DATA_DIR</key>
    <string>${ROOT}/src/data/v31-production</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/desk_support_wrapper.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/desk_support_wrapper.err.log</string>
  <key>ThrottleInterval</key>
  <integer>30</integer>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
sleep 1
if ! launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENTS}/${PLIST}" 2>/dev/null; then
  echo "WARN ${LABEL} already loaded — skipping bootstrap"
fi
launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
# Avoid kickstart -k (SIGKILL) — wrapper sleep handles KeepAlive restart races.
launchctl kickstart "gui/$(id -u)/${LABEL}" 2>/dev/null || true

echo "Installed ${PLIST} — logs: ${LOG_DIR}/desk_support_wrapper.log"
