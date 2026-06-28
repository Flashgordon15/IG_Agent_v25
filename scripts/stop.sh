#!/bin/bash
# v31 APP_MODE shutdown — symmetrical with start.sh (account-scoped session lock).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="${ROOT}/.venv/bin:${PATH}"
export PYTHONPATH="${ROOT}/src"

APP_MODE="${APP_MODE:-}"
MODE_ARG=""
PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3 || true)"
fi

usage() {
  echo "Usage: $0 [--mode DEMO|LIVE|TESTBED|all]" >&2
  echo "  Uses IG_ACCOUNT_SCOPE from env or derives via credentials (same as start.sh)." >&2
  echo "  Default mode: APP_MODE env, else production scope (DEMO account resolve)." >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE_ARG="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [[ -n "${MODE_ARG}" ]]; then
  APP_MODE="$(printf '%s' "${MODE_ARG}" | tr '[:lower:]' '[:upper:]')"
elif [[ -n "${APP_MODE}" ]]; then
  APP_MODE="$(printf '%s' "${APP_MODE}" | tr '[:lower:]' '[:upper:]')"
else
  APP_MODE="DEMO"
fi

shutdown_one() {
  local mode="$1"
  echo "🛑 Shutting down session (APP_MODE=${mode})..."
  if ! "${PY}" -m runtime.session_lock shutdown --mode "${mode}"; then
    local rc=$?
    if [[ "${rc}" -eq 1 ]]; then
      echo "ℹ️  no active session for this account_scope (mode=${mode})"
    else
      echo "❌ shutdown failed for mode=${mode} (exit ${rc})" >&2
      return "${rc}"
    fi
    return 0
  fi
}

case "${APP_MODE}" in
  ALL)
    shutdown_one TESTBED || true
    shutdown_one DEMO || true
    ;;
  DEMO|LIVE|TESTBED)
    shutdown_one "${APP_MODE}"
    ;;
  *)
    echo "ERROR: invalid mode ${APP_MODE} — expected DEMO, LIVE, TESTBED, or all" >&2
    exit 1
    ;;
esac

echo "✅ Shutdown complete."
