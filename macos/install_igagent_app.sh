#!/bin/bash
# Create / refresh macos/IGAgent.app bundle (one-click launcher, no terminal).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${ROOT}/macos/IGAgent.app"
MACOS_BIN="${APP}/Contents/MacOS/IGAgent"
RES="${APP}/Contents/Resources"
SUPERVISOR="${ROOT}/macos/launcher/IGAgentSupervisor"

rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${RES}/Scripts"

cat > "${APP}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>IGAgent</string>
  <key>CFBundleIdentifier</key>
  <string>com.igagent.launcher</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>IG Agent</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>41.0</string>
  <key>CFBundleVersion</key>
  <string>41</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>IG Agent opens the dashboard in your browser after launch.</string>
</dict>
</plist>
PLIST

chmod +x "${ROOT}/macos/launcher/"*.sh "${ROOT}/macos/supervisor/"*.sh 2>/dev/null || true

BUILT_SWIFT=0
if "${ROOT}/macos/supervisor/build_swift.sh" 2>/dev/null; then
  cp "${SUPERVISOR}" "${MACOS_BIN}"
  chmod +x "${MACOS_BIN}"
  BUILT_SWIFT=1
  echo "Using Swift supervisor binary as app entry point"
else
  cat > "${MACOS_BIN}" <<'ENTRY'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../../../.." && pwd)"
export IG_AGENT_ROOT="${IG_AGENT_ROOT:-${ROOT}}"
LAUNCHER="${ROOT}/macos/launcher"
if [[ -x "${LAUNCHER}/IGAgentSupervisor" ]]; then
  exec "${LAUNCHER}/IGAgentSupervisor"
fi
exec /bin/bash "${LAUNCHER}/igagent_launcher.sh"
ENTRY
  chmod +x "${MACOS_BIN}"
  echo "WARN: Swift build failed — app uses bash fallback wrapper"
fi

for s in agent_kill.sh agent_start.sh agent_verify.sh agent_gui.sh agent_lib.sh \
         igagent_launcher.sh lib_notify.sh launcher_core.py; do
  ln -sf "${ROOT}/macos/launcher/${s}" "${RES}/Scripts/${s}"
done

if command -v go >/dev/null 2>&1; then
  (cd "${ROOT}/macos/supervisor" && ./build.sh) || true
fi

echo "✅ Installed ${APP}"
echo "   Double-click: open ${APP}"
echo "   CLI:          ${ROOT}/macos/launcher/launch_agent.sh"
if (( BUILT_SWIFT == 1 )); then
  echo "   Supervisor:   ${SUPERVISOR} (Swift native)"
fi
