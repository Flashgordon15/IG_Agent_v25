#!/usr/bin/env bash
# Arm com.igagent.v32.dual launchd watchdog WITHOUT restarting/unpausing engines.
#
# Safe while desk is flat+paused and A2 CFD hard_block is active.
# Does NOT install/enable legacy com.igagent.v25.watchdog.
# Dual-mode watchdog observes :8080/:8081 and defers single-engine restarts.
#
# Usage:
#   ./scripts/install_v32_dual_watchdog.sh          # generate plist + bootstrap
#   ./scripts/install_v32_dual_watchdog.sh status
#   ./scripts/install_v32_dual_watchdog.sh bootout  # unload dual only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
export CORE_DETACHED="${CORE_DETACHED:-false}"

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
LABEL="com.igagent.v32.dual"
LEGACY_LABEL="com.igagent.v25.watchdog"
DATA_ROOT="${ROOT}/src/data/v31-production"
SHARED_STATE="${DATA_ROOT}/state"
PLIST_DST="${SHARED_STATE}/com.igagent.v32.dual.plist"
PLIST_SRC="${ROOT}/scripts/com.igagent.v32.dual.plist"
MARKER="${SHARED_STATE}/v32_dual_supervision.json"
LEGACY_PAUSED="${SHARED_STATE}/v32_legacy_watchdog_paused.json"
PY="${ROOT}/.venv/bin/python3"
[[ -x "${PY}" ]] || PY="$(command -v python3)"

CMD="${1:-install}"

_status() {
  echo "=== v32 dual watchdog status ==="
  if [[ -f "${MARKER}" ]]; then
    echo "marker: present (${MARKER})"
  else
    echo "marker: ABSENT"
  fi
  if [[ -f "${LEGACY_PAUSED}" ]]; then
    echo "legacy pause marker: present"
  else
    echo "legacy pause marker: absent"
  fi
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    echo "launchd ${LABEL}: LOADED"
    launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null | grep -E 'state =|pid =|path =|last exit' | head -20
  else
    echo "launchd ${LABEL}: not loaded"
  fi
  if launchctl print "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1; then
    echo "WARNING: legacy ${LEGACY_LABEL} still LOADED — bootout required"
  else
    echo "legacy ${LEGACY_LABEL}: not loaded (good)"
  fi
  pgrep -fl 'watchdog_launchd.py|scripts/watchdog.sh' 2>/dev/null | grep -v watchdogd || echo "(no dual watchdog process listed yet)"
}

_bootout_dual() {
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  if [[ -f "${PLIST_DST}" ]]; then
    launchctl bootout "${DOMAIN}" "${PLIST_DST}" 2>/dev/null || true
    launchctl unload "${PLIST_DST}" 2>/dev/null || true
  fi
  # TERM only — never kill -9 / never kickstart -k
  pkill -TERM -f "${ROOT}/scripts/watchdog_launchd.py" 2>/dev/null || true
  sleep 1
}

_ensure_markers() {
  mkdir -p "${SHARED_STATE}" "${DATA_ROOT}/logs"
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  if [[ ! -f "${MARKER}" ]]; then
    printf '%s\n' "{\"dual_port\":true,\"ports\":[8080,8081],\"accounts\":[\"Z6BAH4\",\"Z6BAH3\"],\"started_at\":\"${ts}\",\"legacy_watchdog_paused\":true,\"legacy_plist_disabled\":true,\"source\":\"install_v32_dual_watchdog\"}" > "${MARKER}"
  fi
  if [[ ! -f "${LEGACY_PAUSED}" ]]; then
    printf '%s\n' "{\"paused_at\":\"${ts}\",\"legacy_label\":\"${LEGACY_LABEL}\",\"reason\":\"install_v32_dual_watchdog\",\"plist_disabled\":true}" > "${LEGACY_PAUSED}"
  fi
  launchctl bootout "${DOMAIN}/${LEGACY_LABEL}" 2>/dev/null || true
}

_write_plist() {
  mkdir -p "${SHARED_STATE}" "${DATA_ROOT}/logs"
  cat > "${PLIST_DST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${ROOT}/scripts/watchdog_launchd.py</string>
    <string>--dual-port</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>IG_AGENT_ROOT</key>
    <string>${ROOT}</string>
    <key>PYTHONPATH</key>
    <string>${ROOT}/src</string>
    <key>APP_MODE</key>
    <string>DEMO</string>
    <key>IG_AGENT_CONFIG</key>
    <string>config/config_v31_demo_throughput.json</string>
    <key>IG_V32_DUAL_PORT</key>
    <string>1</string>
    <key>IG_V32_WATCH_PORTS</key>
    <string>8080,8081</string>
    <key>CORE_DETACHED</key>
    <string>${CORE_DETACHED}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${DATA_ROOT}/logs/watchdog_v32_dual.log</string>
  <key>StandardErrorPath</key>
  <string>${DATA_ROOT}/logs/watchdog_v32_dual.log</string>
</dict>
</plist>
EOF
  cp "${PLIST_DST}" "${PLIST_SRC}" 2>/dev/null || true
  echo "Wrote ${PLIST_DST}"
}

_install() {
  local opens
  opens="$(curl -sf --max-time 3 http://127.0.0.1:8080/api/positions/live 2>/dev/null | "${PY}" -c 'import sys,json; d=json.load(sys.stdin); print(int(d.get("count") or 0))' 2>/dev/null || echo "?")"
  if [[ "${opens}" != "0" && "${opens}" != "?" ]]; then
    echo "REFUSE: broker opens=${opens} — do not arm while book is live without operator force" >&2
    exit 2
  fi

  echo "Generating dual plist with RunAtLoad+KeepAlive (observer; no single-engine fight)..."
  _write_plist
  _ensure_markers
  _bootout_dual
  echo "Bootstrapping ${LABEL} (no kickstart -k / no SIGKILL)..."
  if ! launchctl bootstrap "${DOMAIN}" "${PLIST_DST}" 2>/tmp/v32_dual_bootstrap.err; then
    echo "bootstrap failed:" >&2
    cat /tmp/v32_dual_bootstrap.err >&2 || true
    if ! launchctl load "${PLIST_DST}" 2>>/tmp/v32_dual_bootstrap.err; then
      echo "launchctl bootstrap/load FAILED" >&2
      cat /tmp/v32_dual_bootstrap.err >&2 || true
      exit 1
    fi
  fi
  launchctl enable "${DOMAIN}/${LABEL}" 2>/dev/null || true
  # Prefer kickstart WITHOUT -k (no SIGKILL)
  launchctl kickstart "${DOMAIN}/${LABEL}" 2>/dev/null || true
  sleep 2
  _status
  echo "Done. Engines were not restarted; pause/A2 posture unchanged."
}

case "${CMD}" in
  install|arm|on) _install ;;
  status) _status ;;
  bootout|off|stop) _bootout_dual; _status ;;
  -h|--help|help) sed -n '2,14p' "$0" ;;
  *) echo "Unknown: ${CMD} (install|status|bootout)" >&2; exit 1 ;;
esac
