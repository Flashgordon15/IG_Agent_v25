#!/usr/bin/env bash
# Install / remove LaunchAgent for GUI/Desk supervisor (Phase 1+2).
# Default job is observe + score + write SoT. Mutation heals require explicit --heal.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.igagent.gui_desk_supervisor"
DOMAIN="gui/$(id -u)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST_SRC="${ROOT}/scripts/${LABEL}.plist"
PLIST_DST="${LAUNCH_AGENTS}/${LABEL}.plist"
PY="${ROOT}/.venv/bin/python3"
[[ -x "${PY}" ]] || PY="$(command -v python3)"
DATA_ROOT="${ROOT}/src/data/v31-production"
LOG_DIR="${DATA_ROOT}/logs"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install_gui_desk_supervisor.sh            # install + load (same as --enable)
  ./scripts/install_gui_desk_supervisor.sh --enable
  ./scripts/install_gui_desk_supervisor.sh --disable
  ./scripts/install_gui_desk_supervisor.sh --status
  ./scripts/install_gui_desk_supervisor.sh --run-once
  ./scripts/install_gui_desk_supervisor.sh --heal-dry-run
  ./scripts/install_gui_desk_supervisor.sh --g2g

Phase 1: observe + score + write resolve queue + chip/handoff.
Phase 2 heals: allowlisted only via --heal on CLI (never kill -9).
EOF
}

status() {
  echo "=== ${LABEL} ==="
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null | awk '/state =|pid =|path =|last exit code|runs =/{print}'
  else
    echo "not loaded"
  fi
  echo "=== SoT ==="
  ls -la "${DATA_ROOT}/state/gui_supervisor_latest.json" \
         "${DATA_ROOT}/reports/gui_supervisor_latest.md" 2>/dev/null || echo "(no latest files yet)"
  if [[ -f "${DATA_ROOT}/state/gui_supervisor_latest.json" ]]; then
    "${PY}" - <<PY
import json
from pathlib import Path
p = Path("${DATA_ROOT}/state/gui_supervisor_latest.json")
d = json.loads(p.read_text())
a2 = d.get("a2") or {}
print(f"score={d.get('score')} needs_code={d.get('needs_code')} needs_ops={d.get('needs_ops')}")
print(f"a2_marker_active={a2.get('marker_active')} cfd_paused={a2.get('cfd_trading_paused')}")
print(f"checked_at={d.get('checked_at')}")
PY
  fi
  echo "=== agents (should be untouched by this install) ==="
  curl -s --max-time 2 "http://127.0.0.1:8080/api/health" 2>/dev/null \
    | "${PY}" -c "import sys,json; d=json.load(sys.stdin); print('8080 pid', d.get('agent_pid'), 'paused', d.get('trading_paused'))" \
    2>/dev/null || echo "8080 unreachable"
  curl -s --max-time 2 "http://127.0.0.1:8081/api/health" 2>/dev/null \
    | "${PY}" -c "import sys,json; d=json.load(sys.stdin); print('8081 pid', d.get('agent_pid'), 'paused', d.get('trading_paused'))" \
    2>/dev/null || echo "8081 unreachable"
}

run_once() {
  mkdir -p "${LOG_DIR}" "${DATA_ROOT}/state" "${DATA_ROOT}/reports"
  export PYTHONPATH="${ROOT}/src"
  export IG_DATA_ROOT="${DATA_ROOT}"
  export IG_AGENT_DATA_DIR="${DATA_ROOT}"
  export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
  cd "${ROOT}"
  "${PY}" "${ROOT}/scripts/gui_desk_supervisor.py" "$@"
}

heal_dry_run() {
  run_once --heal-dry-run
}

g2g() {
  mkdir -p "${LOG_DIR}" "${DATA_ROOT}/state" "${DATA_ROOT}/reports"
  export PYTHONPATH="${ROOT}/src"
  export IG_DATA_ROOT="${DATA_ROOT}"
  export IG_AGENT_DATA_DIR="${DATA_ROOT}"
  export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
  cd "${ROOT}"
  "${PY}" "${ROOT}/scripts/verify_gui_desk_supervisor_g2g.py" --heal-dry-run
}

disable() {
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  launchctl disable "${DOMAIN}/${LABEL}" 2>/dev/null || true
  rm -f "${PLIST_DST}"
  echo "Disabled ${LABEL}. Trading agents untouched."
  echo "Re-enable: ${ROOT}/scripts/install_gui_desk_supervisor.sh --enable"
}

enable() {
  mkdir -p "${LAUNCH_AGENTS}" "${LOG_DIR}" "${DATA_ROOT}/state" "${DATA_ROOT}/reports"
  chmod +x "${ROOT}/scripts/gui_desk_supervisor.py"
  chmod +x "${ROOT}/scripts/install_gui_desk_supervisor.sh"

  sed \
    -e "s|__REPO_ROOT__|${ROOT}|g" \
    -e "s|__PYTHON_BIN__|${PY}|g" \
    "${PLIST_SRC}" >"${PLIST_DST}"

  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
  launchctl enable "${DOMAIN}/${LABEL}" 2>/dev/null || true
  # Soft kickstart only — never -k; does not touch trading agents
  launchctl kickstart "${DOMAIN}/${LABEL}" 2>/dev/null || true

  echo "✅ GUI desk supervisor LaunchAgent loaded:"
  echo "   ${PLIST_DST}"
  echo "   StartInterval=120s · observe + score (heal via CLI --heal)"
  echo "Disable: ${ROOT}/scripts/install_gui_desk_supervisor.sh --disable"

  # First write immediately (do not wait for interval)
  run_once || true
  status
}

case "${1:-}" in
  ""|--enable)
    enable
    ;;
  --disable)
    disable
    ;;
  --status)
    status
    ;;
  --run-once)
    run_once
    ;;
  --heal-dry-run)
    heal_dry_run
    ;;
  --g2g)
    g2g
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
