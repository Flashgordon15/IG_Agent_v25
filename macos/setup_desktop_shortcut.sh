#!/bin/bash
# Create Desktop shortcut to IG Agent v31 launcher app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_SRC="${ROOT}/macos/IGAgentLauncher.app"
DESKTOP="${HOME}/Desktop"
LINK_NAME="IG Agent v31.app"
TARGET="${DESKTOP}/${LINK_NAME}"

if [[ ! -d "${APP_SRC}" ]]; then
  echo "ERROR: app bundle missing at ${APP_SRC}" >&2
  exit 1
fi

chmod +x "${APP_SRC}/Contents/MacOS/IGAgentLauncher" 2>/dev/null || true
chmod +x "${ROOT}/macos/launcher/launch_agent.sh" 2>/dev/null || true

if [[ -e "${TARGET}" || -L "${TARGET}" ]]; then
  rm -rf "${TARGET}"
fi

ln -s "${APP_SRC}" "${TARGET}"
echo "✅ Desktop shortcut: ${TARGET} -> ${APP_SRC}"
