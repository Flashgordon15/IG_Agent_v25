#!/usr/bin/env bash
# Install / refresh IG Agent Flight Deck on the macOS Desktop (double-click launcher)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_APP="${ROOT}/launcher/IG Agent Flight Deck.app"
DESKTOP_APP="${HOME}/Desktop/IG Agent Flight Deck.app"

if [[ ! -d "${SRC_APP}/Contents/MacOS" ]]; then
  echo "ERROR: missing app bundle at ${SRC_APP}"
  exit 1
fi

chmod +x "${SRC_APP}/Contents/MacOS/launcher"
chmod +x "${ROOT}/flight_deck_launch.sh"

rm -rf "${DESKTOP_APP}"
ditto "${SRC_APP}" "${DESKTOP_APP}"
xattr -cr "${DESKTOP_APP}" 2>/dev/null || true
chmod +x "${DESKTOP_APP}/Contents/MacOS/launcher"

echo "Installed: ${DESKTOP_APP}"
echo "Double-click 'IG Agent Flight Deck' on your Desktop to launch."
echo "Logs: ${ROOT}/src/data/logs/desktop_flight_deck.log"

osascript -e 'display notification "Desktop shortcut ready — double-click to launch." with title "IG Agent Flight Deck"' 2>/dev/null || true
