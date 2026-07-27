#!/bin/bash
# Refresh macos/IGAgent.app — legacy name retained; launches Trading Desk v31.1 (not :8787 Flight Deck).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${ROOT}/macos/IGAgent.app"
MACOS_BIN="${APP}/Contents/MacOS/IGAgent"
RES="${APP}/Contents/Resources"
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
  <string>com.igagent.trading-desk</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>IG Trading Desk</string>
  <key>CFBundleDisplayName</key>
  <string>IG Trading Desk</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>31.1</string>
  <key>CFBundleVersion</key>
  <string>31.1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>IG Trading Desk opens the native v31.1 trading agent dashboard.</string>
</dict>
</plist>
PLIST

if [[ -f "${ICON_SRC}" ]]; then
  cp "${ICON_SRC}" "${RES}/AppIcon.icns"
fi

cat > "${MACOS_BIN}" <<'ENTRY'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../../../.." && pwd)"
exec /bin/bash "${ROOT}/scripts/trading_desk_silent.sh"
ENTRY
chmod +x "${MACOS_BIN}"

echo "✅ Installed ${APP} (redirects to Trading Desk v31.1)"
