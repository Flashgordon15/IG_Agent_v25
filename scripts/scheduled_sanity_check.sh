#!/usr/bin/env bash
# IG Agent v29.1 — Nightly Sanity Check (cron-safe)
#
# Runs the system_state_sanity_checker and certification harness against the
# pure-math risk bracket engine. Does NOT touch the live agent process.
#
# Crontab entry (install with: crontab -e):
#   0 0 * * * /Users/chrisgordon/Projects/IG_Agent_v25/scripts/scheduled_sanity_check.sh
#
set -euo pipefail

ROOT="/Users/chrisgordon/Projects/IG_Agent_v25"
cd "$ROOT"

export PYTHONPATH="${ROOT}/src"
export PATH="${ROOT}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export VIRTUAL_ENV="${ROOT}/.venv"
export PYTHONDONTWRITEBYTECODE=1

PY="${ROOT}/.venv/bin/python3"
LOG_DIR="${ROOT}/logs"
LOG_FILE="${LOG_DIR}/cron_sanity.log"

mkdir -p "$LOG_DIR"

MAX_LOG_SIZE=$((5 * 1024 * 1024))
if [[ -f "$LOG_FILE" ]] && [[ $(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0) -gt $MAX_LOG_SIZE ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.prev"
fi

{
  echo "================================================================"
  echo "  Nightly Sanity Check — $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "================================================================"
  echo ""

  echo "[1/3] System State Sanity Checker (10,000 ticks)..."
  "$PY" scripts/system_state_sanity_checker.py --ticks 10000
  SC_EXIT=$?
  echo ""

  echo "[2/3] Multi-Asset Stress Harness..."
  "$PY" scripts/multi_asset_stress_harness.py
  MA_EXIT=$?
  echo ""

  echo "[3/3] Feature Flag Status..."
  "$PY" scripts/staging_feature_controller.py status
  echo ""

  if [[ $SC_EXIT -eq 0 ]] && [[ $MA_EXIT -eq 0 ]]; then
    echo "NIGHTLY VERDICT: ALL CHECKS PASSED"
  else
    echo "NIGHTLY VERDICT: FAILURES DETECTED (sanity=$SC_EXIT stress=$MA_EXIT)"
  fi
  echo ""
} >> "$LOG_FILE" 2>&1
