#!/usr/bin/env bash
# Full ML retrain: OHLC seed → multi-market replay → dataset → train → analyse.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src"

PY="python3"
for candidate in \
  "${ROOT}/.venv/bin/python3" \
  "${ROOT}/venv/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
    PY="${candidate}"
    break
  fi
done

LOG="${ROOT}/src/data/logs/ml_retrain_pipeline.log"
mkdir -p "$(dirname "${LOG}")"

{
  echo "=== ML retrain pipeline $(date '+%Y-%m-%d %H:%M:%S') ==="

  echo "[1/5] Seed Yahoo OHLC for enabled instruments…"
  "${PY}" src/data/ohlc_yahoo_seeder.py 2>&1 || true

  echo "[2/5] Replay all enabled markets…"
  "${PY}" scripts/replay_signals.py --all

  echo "[3/5] Analyse replay…"
  "${PY}" scripts/analyse_replay.py

  echo "[4/5] Build training dataset…"
  "${PY}" scripts/build_training_dataset.py

  echo "[5/5] Train ML model…"
  "${PY}" scripts/train_ml_model.py

  echo "=== Done $(date '+%Y-%m-%d %H:%M:%S') ==="
} 2>&1 | tee -a "${LOG}"
