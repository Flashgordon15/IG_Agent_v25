#!/usr/bin/env bash
# Start FastAPI dashboard server on port 8080 (separate from trading loop).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export IG_AGENT_ROOT="$ROOT"
export PYTHONPATH="${ROOT}/src"

PYTHON="${PYTHON:-python3}"
if [[ -x "${ROOT}/.venv/bin/python3" ]]; then
  PYTHON="${ROOT}/.venv/bin/python3"
elif [[ -x "${ROOT}/venv/bin/python3" ]]; then
  PYTHON="${ROOT}/venv/bin/python3"
fi

exec "$PYTHON" -m api.server --host 127.0.0.1 --port 8080
