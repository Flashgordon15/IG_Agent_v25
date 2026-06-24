#!/usr/bin/env bash
# IG Agent v30 — Cyber-Quantum Terminal background launcher (port 3000).
# Autonomous UI daemon — no operator terminal interaction required.

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TERMINAL_DIR="${REPO_ROOT}/terminal"
UI_PORT=3000
UI_URL="http://127.0.0.1:${UI_PORT}"
HEALTH_URLS=("${UI_URL}/" "${UI_URL}/health")
PID_FILE="${REPO_ROOT}/src/data/state/ui_terminal_3000.pid"
LOG_FILE="${REPO_ROOT}/src/data/logs/ui_terminal_3000.log"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

log() {
  printf '[UI-BG] %s\n' "$*" | tee -a "$LOG_FILE"
}

resolve_npm() {
  if [ -x "${TERMINAL_DIR}/node_modules/.bin/next" ]; then
    printf '%s' "${TERMINAL_DIR}/node_modules/.bin/next"
    return 0
  fi
  command -v npm
}

if [ ! -f "${TERMINAL_DIR}/package.json" ] || [ ! -f "${TERMINAL_DIR}/next.config.ts" ]; then
  log "FATAL: terminal frontend not found at ${TERMINAL_DIR}"
  exit 1
fi

log "clearing stale bindings on :${UI_PORT}"
lsof -t -i:"${UI_PORT}" 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

cd "${TERMINAL_DIR}"
log "purging .next cache"
rm -rf .next

if [ ! -d node_modules ]; then
  log "installing npm dependencies"
  npm install >>"$LOG_FILE" 2>&1
fi

log "launching detached Next.js dev daemon on :${UI_PORT}"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup sh -c "cd '${TERMINAL_DIR}' && exec ./node_modules/.bin/next dev --port ${UI_PORT}" >>"$LOG_FILE" 2>&1 &
else
  nohup sh -c "cd '${TERMINAL_DIR}' && exec ./node_modules/.bin/next dev --port ${UI_PORT}" >>"$LOG_FILE" 2>&1 &
fi
UI_PID=$!
disown -h "$UI_PID" 2>/dev/null || true
echo "$UI_PID" >"$PID_FILE"
log "background PID=${UI_PID}"

VERIFY_OK=0
for i in $(seq 1 30); do
  for url in "${HEALTH_URLS[@]}"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    if [ "$code" = "200" ]; then
      VERIFY_OK=1
      log "verified HTTP 200 from ${url} (attempt ${i})"
      break 2
    fi
  done
  sleep 1
done

if [ "$VERIFY_OK" -ne 1 ]; then
  log "FATAL: UI did not return HTTP 200 within 30s"
  exit 1
fi

LISTEN_PID="$(lsof -t -iTCP:${UI_PORT} -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [ -n "$LISTEN_PID" ]; then
  echo "$LISTEN_PID" >"$PID_FILE"
  log "listening PID=${LISTEN_PID}"
fi

AGENT_CODE="$(curl -s -o /dev/null -w '%{http_code}' "${UI_URL}/api/health" 2>/dev/null || echo 000)"
log "agent proxy /api/health → HTTP ${AGENT_CODE}"
log "UI background service online at ${UI_URL}"

install_launchd() {
  local plist_src="${REPO_ROOT}/scripts/com.igagent.v30.ui.plist"
  local plist_dst="${HOME}/Library/LaunchAgents/com.igagent.v30.ui.plist"
  local domain="gui/$(id -u)"
  sed "s|__REPO_ROOT__|${REPO_ROOT}|g" "$plist_src" >"$plist_dst"
  chmod +x "${REPO_ROOT}/scripts/ui_terminal_daemon.sh"
  launchctl bootout "$domain" "$plist_dst" 2>/dev/null || true
  launchctl bootstrap "$domain" "$plist_dst"
  launchctl enable "$domain/com.igagent.v30.ui" 2>/dev/null || true
  log "installed LaunchAgent com.igagent.v30.ui → ${plist_dst}"
}

case "${1:-}" in
  --install-launchd)
    install_launchd
    ;;
esac

exit 0
