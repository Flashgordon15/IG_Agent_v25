#!/usr/bin/env bash
# LaunchAgent entry — start Quantum Terminal only when :3000 is not listening.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
UI_PORT=3000

if lsof -iTCP:"${UI_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  exit 0
fi

exec "${REPO_ROOT}/scripts/start_ui_background.sh"
