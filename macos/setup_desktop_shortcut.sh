#!/bin/bash
# Create Desktop shortcut to IG Agent launcher app (v41).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_SRC="${ROOT}/macos/IGAgent.app"
DESKTOP="${HOME}/Desktop"
LINK_NAME="IG Agent.app"
TARGET="${DESKTOP}/${LINK_NAME}"

if [[ ! -d "${APP_SRC}" ]]; then
  echo "Building app bundle…"
  "${ROOT}/macos/install_igagent_app.sh"
fi

chmod +x "${APP_SRC}/Contents/MacOS/IGAgent" 2>/dev/null || true
chmod +x "${ROOT}/macos/launcher/"*.sh 2>/dev/null || true

if [[ -e "${TARGET}" || -L "${TARGET}" ]]; then
  rm -rf "${TARGET}"
fi

ln -s "${APP_SRC}" "${TARGET}"
echo "✅ Desktop shortcut: ${TARGET} -> ${APP_SRC}"
