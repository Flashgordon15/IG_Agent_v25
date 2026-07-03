#!/bin/bash
# Create Desktop shortcut to Iron Cage Flight Deck (native pywebview shell).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_SRC="${ROOT}/macos/IGAgent.app"
DESKTOP="${HOME}/Desktop"
LINK_NAME="Iron Cage Flight Deck.app"
LEGACY_NAME="IG Agent.app"
TARGET="${DESKTOP}/${LINK_NAME}"

"${ROOT}/macos/install_igagent_app.sh"

chmod +x "${APP_SRC}/Contents/MacOS/IGAgent" 2>/dev/null || true
chmod +x "${ROOT}/macos/launcher/"*.sh 2>/dev/null || true

# Remove legacy shortcuts that still open the old Swift splash / browser flow.
for old in "${LEGACY_NAME}" "Launch_IG_Agent.command"; do
  if [[ -e "${DESKTOP}/${old}" || -L "${DESKTOP}/${old}" ]]; then
    rm -rf "${DESKTOP}/${old}"
    echo "Removed legacy Desktop item: ${old}"
  fi
done

if [[ -e "${TARGET}" || -L "${TARGET}" ]]; then
  rm -rf "${TARGET}"
fi

ln -sf "${APP_SRC}" "${TARGET}"

# Refresh Finder icon cache for the symlink (best-effort).
touch "${APP_SRC}"
echo "✅ Desktop shortcut: ${TARGET} -> ${APP_SRC}"
echo "   Launches: desktop_flight_deck.sh → cockpit.desktop_app_shell"
