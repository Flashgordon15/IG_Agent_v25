#!/usr/bin/env bash
# Daily loss autopsy (+ optional ml_strategy_review + shadow loss loop) — safe while locked.
#
# Usage:
#   ./scripts/run_daily_loss_autopsy.sh                  # today London day
#   ./scripts/run_daily_loss_autopsy.sh 2026-07-24
#   ./scripts/run_daily_loss_autopsy.sh 2026-07-24 --with-review
#   ./scripts/run_daily_loss_autopsy.sh 2026-07-24 --with-review --with-shadow
#
# Day is a positional YYYY-MM-DD (not --day). Never unlocks trading.
# Read-only reports under data_dir()/reports/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
PY="${ROOT}/.venv/bin/python3"
if [ ! -x "${PY}" ]; then
  PY="$(command -v python3)"
fi

DAY=""
WITH_REVIEW=0
WITH_SHADOW=0
for arg in "$@"; do
  case "$arg" in
    --with-review) WITH_REVIEW=1 ;;
    --with-shadow) WITH_SHADOW=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      if [[ -z "$DAY" && "$arg" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        DAY="$arg"
      else
        echo "Unknown arg: $arg" >&2
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$DAY" ]]; then
  DAY="$("${PY}" -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d'))")"
fi

echo "=== loss autopsy day=${DAY} ==="
"${PY}" scripts/trade_lifecycle_witness.py --loss-autopsy --day "${DAY}" --write

if [[ "${WITH_REVIEW}" -eq 1 ]]; then
  echo "=== ml_strategy_review day=${DAY} ==="
  "${PY}" scripts/ml_strategy_review.py --day "${DAY}"
fi

if [[ "${WITH_SHADOW}" -eq 1 ]]; then
  echo "=== shadow loss loop day=${DAY} ==="
  "${PY}" scripts/shadow_loss_loop.py --day "${DAY}"
fi

echo "Done. Reports under data_dir()/reports/ (loss_autopsy_${DAY}.* / ml_strategy_review_${DAY}.* / shadow_loss_loop_${DAY}.*)"
