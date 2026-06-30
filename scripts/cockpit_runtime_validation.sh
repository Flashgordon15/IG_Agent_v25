#!/bin/bash
# Full IG Cockpit runtime validation — unit tests + launch contract + optional live probe.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${ROOT}/logs/cockpit_runtime_validation.log"
COCKPIT="${ROOT}/gui/ig_cockpit"
PORT="${IG_API_PORT:-8080}"
TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

mkdir -p "${ROOT}/logs"

{
  echo "========== IG Cockpit Runtime Validation ${TS} =========="
  echo ""
  echo "[Pre-flight audit — read-only]"
  echo "  Market sessions: operator must confirm flat before deploy"
  if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "  Agent on :${PORT}: UP"
    curl -sf --max-time 5 "http://127.0.0.1:${PORT}/api/gui_status" | head -c 200
    echo "..."
  else
    echo "  Agent on :${PORT}: DOWN (offline tests only)"
  fi
  echo ""

  echo "[1] Cockpit TypeScript runtime tests (vitest)"
  cd "${COCKPIT}"
  if [[ ! -d node_modules/vitest ]]; then
    npm install --save-dev vitest@^3.0.0 2>&1
  fi
  npm run test:runtime 2>&1
  echo ""

  echo "[2] Python launch contract tests"
  cd "${ROOT}"
  PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_cockpit_runtime_validation.py -q 2>&1
  echo ""

  echo "[3] Cockpit build sanity"
  cd "${COCKPIT}"
  npm run build 2>&1
  echo ""

  echo "[4] Supervisor launch script syntax"
  bash -n "${ROOT}/macos/launcher/agent_gui.sh"
  bash -n "${ROOT}/macos/launcher/agent_start.sh"
  echo "  agent_gui.sh / agent_start.sh: OK"
  echo ""

  echo "========== VALIDATION COMPLETE ${TS} =========="
} 2>&1 | tee "${LOG}"

echo "Log written to ${LOG}"
