#!/usr/bin/env bash
# Install / remove LaunchAgent that keeps Trading Desk UI (Quantum :3000 + pywebview shell) always on.
# Viewer/UI only — does not load agent dual launchd or restart :8080/:8081.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.igagent.trading_desk"
UI_LABEL="com.igagent.v30.ui"
DOMAIN="gui/$(id -u)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST_SRC="${ROOT}/scripts/${LABEL}.plist"
PLIST_DST="${LAUNCH_AGENTS}/${LABEL}.plist"
UI_PLIST_SRC="${ROOT}/scripts/${UI_LABEL}.plist"
UI_PLIST_DST="${LAUNCH_AGENTS}/${UI_LABEL}.plist"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install_trading_desk_always_on.sh          # install + load KeepAlive
  ./scripts/install_trading_desk_always_on.sh --enable
  ./scripts/install_trading_desk_always_on.sh --disable
  ./scripts/install_trading_desk_always_on.sh --status
EOF
}

status() {
  echo "=== ${LABEL} ==="
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null | awk '/state =|pid =|path =|last exit code/{print}'
  else
    echo "not loaded"
  fi
  echo "=== ${UI_LABEL} (Quantum :3000) ==="
  if launchctl print "${DOMAIN}/${UI_LABEL}" >/dev/null 2>&1; then
    launchctl print "${DOMAIN}/${UI_LABEL}" 2>/dev/null | awk '/state =|pid =|path =|last exit code/{print}'
  else
    echo "not loaded"
  fi
  echo "=== ports ==="
  lsof -nP -iTCP:3000 -sTCP:LISTEN 2>/dev/null | head -3 || echo ":3000 not listening"
  pgrep -lf 'cockpit.desktop_app_shell.*3000' 2>/dev/null || echo "desktop_app_shell not running"
}

disable() {
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  launchctl disable "${DOMAIN}/${LABEL}" 2>/dev/null || true
  # Leave com.igagent.v30.ui alone unless explicitly asked — disable both for full UI always-off
  if [[ "${1:-}" == "--all-ui" ]]; then
    launchctl bootout "${DOMAIN}/${UI_LABEL}" 2>/dev/null || true
    launchctl disable "${DOMAIN}/${UI_LABEL}" 2>/dev/null || true
  fi
  echo "Disabled ${LABEL} (Desk shell KeepAlive). Agents untouched."
  echo "Re-enable: ${ROOT}/scripts/install_trading_desk_always_on.sh --enable"
}

enable() {
  chmod +x "${ROOT}/scripts/trading_desk_viewer_keepalive.sh"
  chmod +x "${ROOT}/scripts/ui_terminal_daemon.sh"
  mkdir -p "${LAUNCH_AGENTS}" "${ROOT}/logs"

  sed "s|__REPO_ROOT__|${ROOT}|g" "${PLIST_SRC}" >"${PLIST_DST}"
  sed "s|__REPO_ROOT__|${ROOT}|g" "${UI_PLIST_SRC}" >"${UI_PLIST_DST}"

  # Quantum Terminal KeepAlive (existing pattern) — UI only
  launchctl bootout "${DOMAIN}" "${UI_PLIST_DST}" 2>/dev/null || true
  launchctl bootstrap "${DOMAIN}" "${UI_PLIST_DST}"
  launchctl enable "${DOMAIN}/${UI_LABEL}" 2>/dev/null || true

  # Trading Desk shell KeepAlive — viewer only
  launchctl bootout "${DOMAIN}" "${PLIST_DST}" 2>/dev/null || true
  launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
  launchctl enable "${DOMAIN}/${LABEL}" 2>/dev/null || true
  # Soft kickstart only (no -k) — never disrupt a live viewer shell or agents
  launchctl kickstart "${DOMAIN}/${LABEL}" 2>/dev/null || true

  echo "✅ Always-on Trading Desk UI loaded:"
  echo "   ${PLIST_DST}"
  echo "   ${UI_PLIST_DST}"
  echo "Disable: ${ROOT}/scripts/install_trading_desk_always_on.sh --disable"
}

case "${1:-}" in
  ""|--enable)
    enable
    ;;
  --disable)
    disable
    ;;
  --disable-all-ui)
    disable --all-ui
    ;;
  --status)
    status
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
