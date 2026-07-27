#!/usr/bin/env bash
# Install (or unload) the weekday daily loss-loop LaunchAgent.
#
# Runs at 21:40 local on Mon–Fri:
#   ./scripts/run_daily_loss_autopsy.sh --with-review --with-shadow
#
# Read-only reports only — never arms trading, never lifts A2, never kills mains.
#
# Usage:
#   ./scripts/install_daily_loss_loop.sh           # install + load
#   ./scripts/install_daily_loss_loop.sh --unload  # unload + remove
#   ./scripts/install_daily_loss_loop.sh --dry-run # show rendered plist, no load
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.igagent.daily_loss_loop"
SRC_PLIST="${ROOT}/scripts/${LABEL}.plist"
DEST_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LAUNCH_DOMAIN="gui/$(id -u)"
LOG_DIR="${ROOT}/src/data/v31-production/logs"

MODE="install"
case "${1:-}" in
  --unload) MODE="unload" ;;
  --dry-run) MODE="dry-run" ;;
  -h|--help)
    sed -n '2,16p' "$0"
    exit 0
    ;;
  "") ;;
  *)
    echo "Unknown arg: $1" >&2
    exit 1
    ;;
esac

if [[ ! -f "${SRC_PLIST}" ]]; then
  echo "Missing template: ${SRC_PLIST}" >&2
  exit 1
fi

render_plist() {
  sed "s|__IG_AGENT_ROOT__|${ROOT}|g" "${SRC_PLIST}"
}

if [[ "${MODE}" == "dry-run" ]]; then
  echo "=== dry-run rendered plist ==="
  render_plist
  exit 0
fi

if [[ "${MODE}" == "unload" ]]; then
  launchctl bootout "${LAUNCH_DOMAIN}/${LABEL}" 2>/dev/null || true
  rm -f "${DEST_PLIST}"
  echo "Unloaded ${LABEL} (if present) and removed ${DEST_PLIST}"
  exit 0
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"
chmod +x "${ROOT}/scripts/run_daily_loss_autopsy.sh"

render_plist > "${DEST_PLIST}"
# Replace if already loaded
launchctl bootout "${LAUNCH_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${LAUNCH_DOMAIN}" "${DEST_PLIST}"
launchctl enable "${LAUNCH_DOMAIN}/${LABEL}" 2>/dev/null || true

echo "Installed + loaded ${LABEL}"
echo "  plist: ${DEST_PLIST}"
echo "  log:   ${LOG_DIR}/daily_loss_loop.log"
echo "  when:  Mon–Fri 21:40 local (set Mini TZ to Europe/London)"
echo "  cmd:   run_daily_loss_autopsy.sh --with-review --with-shadow"
echo "Unload: ./scripts/install_daily_loss_loop.sh --unload"
