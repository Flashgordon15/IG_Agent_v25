#!/bin/bash
# IG Agent v31 — full startup contract (DEMO). Logs to logs/launcher.log
set -uo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${LAUNCHER_DIR}/../.." && pwd)"
cd "${ROOT}"

# shellcheck source=lib_notify.sh
source "${LAUNCHER_DIR}/lib_notify.sh"

mkdir -p "${ROOT}/logs"
LOG_FILE="${ROOT}/logs/launcher.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "========== IG Agent v31 Launcher $(date -u +%Y-%m-%dT%H:%M:%SZ) =========="

PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3 || true)"
fi
export PATH="${ROOT}/.venv/bin:${PATH}"
export PYTHONPATH="${ROOT}/src"
export APP_MODE="${APP_MODE:-DEMO}"
PORT="${IG_API_PORT:-8080}"

fail() {
  local msg="$1"
  echo "ERROR: ${msg}"
  alert_launcher "${msg}"
  exit "${2:-1}"
}

notify_launcher "IG Agent v31" "Stopping old agent…"
echo "[STOP] begin"
if ! "${PY}" "${LAUNCHER_DIR}/launcher_core.py" --phase stop --port "${PORT}"; then
  fail "Stop phase failed — port ${PORT} may still be bound" 2
fi
echo "[STOP] ok"

notify_launcher "IG Agent v31" "Cleaning caches…"
echo "[CLEAN] begin"
if ! "${PY}" "${LAUNCHER_DIR}/launcher_core.py" --phase clean; then
  fail "Clean phase failed" 3
fi
echo "[CLEAN] ok"

if [[ "${APP_MODE}" == "DEMO" ]]; then
  notify_launcher "IG Agent v31" "Resetting DEMO session state…"
  echo "[RESET] begin (DEMO)"
  "${PY}" "${LAUNCHER_DIR}/launcher_core.py" --phase reset || fail "DEMO reset failed" 4
  echo "[RESET] ok"
fi

notify_launcher "IG Agent v31" "Running test suite…"
echo "[START] begin — invoking scripts/start.sh"
if ! ./scripts/start.sh --mode "${APP_MODE}"; then
  fail "Test suite or startup failed — see logs/launcher.log and pytest output" 5
fi
echo "[START] ok"

notify_launcher "IG Agent v31" "Verifying health and GUI…"
echo "[VERIFY] begin"
if ! "${PY}" "${LAUNCHER_DIR}/launcher_core.py" --phase verify --port "${PORT}"; then
  fail "Health or /api/gui_status verification failed" 6
fi
echo "[VERIFY] ok"

notify_launcher "IG Agent v31" "Opening dashboard…"
echo "[GUI] begin"
"${PY}" "${LAUNCHER_DIR}/launcher_core.py" --phase gui --port "${PORT}" || true
echo "[GUI] ok"

notify_launcher "IG Agent v31" "Agent ready."
echo "✅ Launcher complete — http://127.0.0.1:${PORT}/"
exit 0
