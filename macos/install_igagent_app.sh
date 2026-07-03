#!/bin/bash
# Create / refresh macos/IGAgent.app — one-click Iron Cage Flight Deck (pywebview shell).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${ROOT}/macos/IGAgent.app"
MACOS_BIN="${APP}/Contents/MacOS/IGAgent"
RES="${APP}/Contents/Resources"
SUPERVISOR="${ROOT}/macos/launcher/IGAgentSupervisor"
ICON_SRC="${ROOT}/macos/assets/AppIcon.icns"

if [[ ! -f "${ICON_SRC}" ]]; then
  if [[ -f "${ROOT}/macos/assets/flight_deck_icon_1024.png" ]]; then
    "${ROOT}/macos/build_app_icon.sh"
  else
    echo "WARN: AppIcon.icns missing — run macos/build_app_icon.sh after adding icon PNG" >&2
  fi
fi

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
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>com.igagent.flightdeck</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Iron Cage Flight Deck</string>
  <key>CFBundleDisplayName</key>
  <string>Iron Cage Flight Deck</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>29.1</string>
  <key>CFBundleVersion</key>
  <string>291</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>Iron Cage Flight Deck opens the native trading cockpit after boot.</string>
</dict>
</plist>
PLIST

if [[ -f "${ICON_SRC}" ]]; then
  cp "${ICON_SRC}" "${RES}/AppIcon.icns"
fi

chmod +x "${ROOT}/macos/launcher/"*.sh "${ROOT}/macos/supervisor/"*.sh 2>/dev/null || true

# Native entry: pywebview Flight Deck shell (not legacy Swift splash / browser open).
cat > "${MACOS_BIN}" <<'ENTRY'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../../../.." && pwd)"
export IG_AGENT_ROOT="${IG_AGENT_ROOT:-${ROOT}}"
export LAUNCHER_DESKTOP=1
export IG_DESKTOP_FLIGHT_DECK=1
export IG_DESKTOP_SHELL_ACTIVE=1
export IG_COCKPIT_URL="${IG_COCKPIT_URL:-http://127.0.0.1:8787/}"
exec /bin/bash "${ROOT}/macos/launcher/desktop_flight_deck.sh" --launch-supervisor
ENTRY
chmod +x "${MACOS_BIN}"

# Legacy supervisor retained for CLI — not used as double-click entry point.
if "${ROOT}/macos/supervisor/build_swift.sh" 2>/dev/null; then
  cp "${SUPERVISOR}" "${RES}/Scripts/IGAgentSupervisor"
  chmod +x "${RES}/Scripts/IGAgentSupervisor"
  echo "Bundled legacy IGAgentSupervisor at Resources/Scripts (CLI only)"
fi

for s in agent_kill.sh agent_start.sh agent_verify.sh agent_gui.sh agent_lib.sh \
         desktop_flight_deck.sh igagent_launcher.sh lib_notify.sh launcher_status.sh launcher_core.py; do
  ln -sf "${ROOT}/macos/launcher/${s}" "${RES}/Scripts/${s}"
done

if command -v go >/dev/null 2>&1; then
  (cd "${ROOT}/macos/supervisor" && ./build.sh) || true
fi

echo "✅ Installed ${APP}"
echo "   Double-click: Iron Cage Flight Deck (pywebview shell + supervisor)"
echo "   Desktop:      ${ROOT}/macos/setup_desktop_shortcut.sh"
