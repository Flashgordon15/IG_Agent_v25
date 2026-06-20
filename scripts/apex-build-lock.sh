#!/usr/bin/env bash
# Apex Monolith — bytecode purge + compile-lock critical v30 hot-path modules.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${AGENT_DIR}"

PY="${AGENT_DIR}/.venv/bin/python3"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

echo "[apex-build-lock] purging workspace bytecode"
find . -name "*.pyc" -delete 2>/dev/null || true
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "[apex-build-lock] compiling v30 modules"
"${PY}" -m py_compile \
  src/signals/indicators.py \
  src/analytics/triage_logger.py \
  src/apex/ipc_bridge.py \
  src/apex/microkernel.py \
  src/system/paths.py \
  src/system/node_profile.py \
  src/trading/trading_loop.py \
  src/signals/signal_engine.py

echo "[apex-build-lock] compileall apex + analytics + signals"
"${PY}" -m compileall -q src/apex src/analytics src/signals

echo "[apex-build-lock] complete"
