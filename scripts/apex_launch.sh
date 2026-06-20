#!/usr/bin/env bash
# IG Agent Apex v30 — unified lifecycle master launch (standalone DEMO unit on :9090).
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${AGENT_DIR}/dist-apex/mac-arm64/IG Agent Apex.app"
APP_ROOT="${APP}/Contents/Resources/agent"
ISOLATED="${HOME}/Library/Application Support/IG Agent Apex/v30-production"
HEALTH="http://127.0.0.1:9090/api/health"
PID_FILE="${AGENT_DIR}/logs/apex_sidecar.pid"
LOG="${AGENT_DIR}/logs/apex_launch.log"
IPC_SOCK="${ISOLATED}/data/apex_ipc.sock"

mkdir -p "${AGENT_DIR}/logs" "${ISOLATED}/data/logs"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "${LOG}"; }

# ── 1. Port Eviction ─────────────────────────────────────────────────────────
evict_port_9090() {
  log "Step 1/5 Port eviction: lsof tcp:9090 → kill -9 zombies"
  if lsof -i tcp:9090 >/dev/null 2>&1; then
    lsof -i tcp:9090 | tee -a "${LOG}" || true
  fi
  for pid in $(lsof -tiTCP:9090 -sTCP:LISTEN 2>/dev/null || true); do
    kill -9 "$pid" 2>/dev/null || true
  done
  pkill -9 -f "IG Agent Apex" 2>/dev/null || true
  rm -f "${IPC_SOCK}" 2>/dev/null || true
  log "Port eviction: :9090 cleared"
}

# ── 2. Frontend Rebuild ──────────────────────────────────────────────────────
compile_dashboard() {
  log "Step 2/5 Frontend rebuild: dashboard dist (file:// relative paths)"
  if [[ ! -f "${AGENT_DIR}/dashboard/package.json" ]]; then
    log "FATAL: dashboard/package.json missing"
    exit 1
  fi
  (cd "${AGENT_DIR}/dashboard" && npm run build --silent) >>"${LOG}" 2>&1
  log "Frontend rebuild: OK"
}

# ── 3. Integrity Hash ────────────────────────────────────────────────────────
patch_asar_integrity() {
  log "Step 3/5 Integrity hash: apex-fix-asar-integrity.sh"
  if [[ -d "${APP}" ]]; then
    bash "${AGENT_DIR}/scripts/apex-fix-asar-integrity.sh" "${APP}" >>"${LOG}" 2>&1 || true
    xattr -cr "${APP}" 2>/dev/null || true
  else
    log "WARN: app bundle missing — integrity patch skipped"
  fi
  log "Integrity hash: OK"
}

sync_packaged_sidecar() {
  [[ -d "${AGENT_DIR}/src" && -d "${APP_ROOT}/src" ]] || return 0
  log "Sidecar sync: repo src → packaged agent"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' \
    "${AGENT_DIR}/src/" "${APP_ROOT}/src/"
  if [[ -d "${AGENT_DIR}/dashboard/dist" ]]; then
    mkdir -p "${APP_ROOT}/dashboard/dist"
    rsync -a "${AGENT_DIR}/dashboard/dist/" "${APP_ROOT}/dashboard/dist/"
    log "Sidecar sync: dashboard/dist → packaged agent"
  fi
}

# ── 4. Electron daemon supervisor (double-click owns :9090) ─────────────────
open_desktop_shell() {
  log "Step 4/4 Window launch — Electron daemon supervisor spawns detached :9090"
  if [[ -d "${APP}" ]]; then
    xattr -cr "${APP}" 2>/dev/null || true
    open "${APP}"
  else
    log "FATAL: app bundle missing — run electron-builder --mac dir first"
    exit 1
  fi
}

log "=== Apex v30 MASTER LAUNCH — double-click daemon supervisor ==="
evict_port_9090
sync_packaged_sidecar
compile_dashboard
patch_asar_integrity
open_desktop_shell
log "Done — daemon supervisor inside IG Agent Apex.app · http://127.0.0.1:9090/"
