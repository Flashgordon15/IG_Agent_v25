#!/usr/bin/env bash
# Strategy isolation verification — Application Layer adversarial stress gate.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}/src"
PY="${ROOT}/.venv/bin/python3"
echo "[STRATEGY-ISOLATION] running adversarial Alpha/Beta/Gamma suite"
"$PY" -m pytest tests/test_strategy_isolation.py -q --tb=short
echo "[STRATEGY-ISOLATION] PASS — exit 0"
