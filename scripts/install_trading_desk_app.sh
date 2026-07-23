#!/usr/bin/env bash
# Install canonical IG Trading Desk v34 on Desktop and retire legacy shortcuts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_APP="${ROOT}/Trading_Desk.app"
DESKTOP="${HOME}/Desktop"
TARGET="${DESKTOP}/Trading_Desk.app"

if [[ ! -d "${SRC_APP}/Contents/MacOS" ]]; then
  echo "ERROR: ${SRC_APP} missing — run from IG_Agent_v25 repo root" >&2
  exit 1
fi

chmod +x "${SRC_APP}/Contents/MacOS/launch" 2>/dev/null || true
chmod +x "${ROOT}/scripts/trading_desk_silent.sh"

# Remove legacy Desktop shortcuts that still open v29 Flight Deck / browser / Apex.
LEGACY_ITEMS=(
  "Iron Cage Flight Deck.app"
  "IG Agent.app"
  "Launch_IG_Agent.command"
  "Launch_Trading_Desk.command"
  "IG Agent Flight Deck.app"
  "IG Agent v29.0.app"
  "IG Apex Cockpit.app"
)
for item in "${LEGACY_ITEMS[@]}"; do
  if [[ -e "${DESKTOP}/${item}" || -L "${DESKTOP}/${item}" ]]; then
    rm -rf "${DESKTOP}/${item}"
    echo "Removed legacy Desktop item: ${item}"
  fi
done

rm -rf "${TARGET}"
ditto "${SRC_APP}" "${TARGET}"
chmod +x "${TARGET}/Contents/MacOS/launch"

# Refresh in-repo legacy bundles so any old Dock/Spotlight entry still opens Trading Desk.
"${ROOT}/macos/install_igagent_app.sh" >/dev/null

touch "${TARGET}"
echo "✅ Desktop Trading Desk installed: ${TARGET}"
echo "   Version: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "${TARGET}/Contents/Info.plist" 2>/dev/null || echo '?')"
echo "   Launches: scripts/trading_desk_silent.sh → multiplex desk :3000/desk (pywebview)"
