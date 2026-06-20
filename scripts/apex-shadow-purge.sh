#!/usr/bin/env bash
# Purge shadow-development bytecode only — never touches production runtime_state.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${AGENT_DIR}"

echo "[apex-shadow-purge] clearing __pycache__ / *.pyc under src/ native/ scripts/"
find src native scripts -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find src native scripts -name '*.pyc' -delete 2>/dev/null || true
rm -f src/data/.ig_agent_v30_shadow.lock 2>/dev/null || true
rm -f src/data/apex_ipc.sock src/data/apex_ipc_shadow.sock 2>/dev/null || true
echo "[apex-shadow-purge] complete (production locks untouched)"
