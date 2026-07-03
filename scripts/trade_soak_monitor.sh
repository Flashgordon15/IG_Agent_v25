#!/bin/bash
# Monitor trading plane until N trades, duration elapses, or failure.
# Usage: trade_soak_monitor.sh [duration_sec] [interval_sec] [target_trades]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DURATION_SEC="${1:-1800}"
INTERVAL_SEC="${2:-60}"
TARGET_TRADES="${3:-10}"
LOG="${ROOT}/logs/trade_soak_monitor.log"
API="http://127.0.0.1:8080"
STDOUT_LOG="${ROOT}/src/data/v31-production/logs/agent_stdout.log"
EVAL_LOG="${ROOT}/src/data/v31-production/logs/strategy_eval.log"
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "${ROOT}/logs"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "${LOG}"; }

stdout_lines() { wc -l < "${STDOUT_LOG}" 2>/dev/null || echo 0; }
eval_lines() { wc -l < "${EVAL_LOG}" 2>/dev/null || echo 0; }

count_trades_since_start() {
  ROOT="${ROOT}" MONITOR_START_TS="${START_TS}" IG_DATA_ROOT="${IG_DATA_ROOT:-${ROOT}/src/data/v31-production}" \
    PYTHONPATH="${ROOT}/src" \
    "${ROOT}/.venv/bin/python3" - <<'PY' 2>/dev/null || echo 0
import os
import sqlite3
from pathlib import Path

start = os.environ.get("MONITOR_START_TS", "")
if not start:
    print(0)
    raise SystemExit
raw = os.environ.get("IG_TRIAGE_DB", "").strip()
if raw:
    db = Path(raw).resolve()
else:
    try:
        from system.node_profile import apply_node_profile_to_environ

        db = apply_node_profile_to_environ().triage_db
    except Exception:
        root = Path(os.environ.get("ROOT", "."))
        db = root / "src" / "analytics" / "triage_v31.db"
if not db.is_file():
    print(0)
    raise SystemExit
conn = sqlite3.connect(str(db))
try:
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM production_orders
        WHERE datetime(created_at) >= datetime(?)
          AND status NOT IN ('REJECTED', 'ERROR', 'CANCELLED')
        """,
        (start,),
    )
    print(int(cur.fetchone()[0] or 0))
finally:
    conn.close()
PY
}

START_STDOUT_LINES="$(stdout_lines)"
START_EVAL_LINES="$(eval_lines)"
export MONITOR_START_TS="${START_TS}"

log "trade_soak_monitor start duration=${DURATION_SEC}s interval=${INTERVAL_SEC}s target_trades=${TARGET_TRADES} stdout_line=${START_STDOUT_LINES} eval_line=${START_EVAL_LINES}"
deadline=$(( $(date +%s) + DURATION_SEC ))
trade_count=0
fail_streak=0

while (( $(date +%s) < deadline )); do
  health="$(curl -s --max-time 8 "${API}/api/health" 2>/dev/null || echo '{}')"
  light="$(curl -s --max-time 5 "${API}/api/health_light" 2>/dev/null || echo '{}')"
  trade_count="$(count_trades_since_start)"
  PYTHONPATH="${ROOT}/src" "${ROOT}/.venv/bin/python3" - <<PY | tee -a "${LOG}"
import json
health = json.loads('''${health}''')
light = json.loads('''${light}''')
bm = (health.get('boot_metrics') or {}).get('system_state') or {}
loops = bm.get('loops') or {}
g5 = (bm.get('gates') or {}).get('G5') or {}
ic = light.get('iron_cage') or health.get('iron_cage') or {}
print(
    f"trade_ready={health.get('trade_ready')} ready={bm.get('ready')} "
    f"accepting_ticks={loops.get('accepting_ticks')} "
    f"G5={g5.get('status')} "
    f"hub_fresh={(light.get('data_feeds') or {}).get('hub')} "
    f"armed={(light.get('routing_state') or {}).get('armed')} "
    f"exec_active={light.get('execution_loop_active')} "
    f"iron_blockers={ic.get('blockers')} "
    f"trades_since_start=${trade_count}"
)
PY

  if (( trade_count >= TARGET_TRADES )); then
    log "trade_soak_monitor complete — ${trade_count} trade(s) recorded (target=${TARGET_TRADES})"
    exit 0
  fi

  cur_stdout="$(stdout_lines)"
  cur_eval="$(eval_lines)"
  if (( cur_eval > START_EVAL_LINES )); then
    if tail -n $((cur_eval - START_EVAL_LINES)) "${EVAL_LOG}" 2>/dev/null | grep -q "Match: True"; then
      log "NEW signal match since monitor start"
    fi
  fi
  if (( cur_stdout > START_STDOUT_LINES )); then
    if tail -n $((cur_stdout - START_STDOUT_LINES)) "${STDOUT_LOG}" 2>/dev/null | grep -qiE "fill|deal_id|order.*placed|execution.*success|Micro-scalp fill|production_orders"; then
      log "NEW trade event in stdout since monitor start"
    fi
    if tail -n $((cur_stdout - START_STDOUT_LINES)) "${STDOUT_LOG}" 2>/dev/null | grep -q "dispatch blocked.*BROKER_STATE_MISMATCH"; then
      log "WARN: BROKER_STATE_MISMATCH dispatch block detected"
    fi
  fi

  if echo "${light}" | grep -q '"trade_ready": true\|"trade_ready":true'; then
    fail_streak=0
  else
    fail_streak=$((fail_streak + 1))
    if (( fail_streak >= 5 )); then
      log "WARN: trade_ready false for ${fail_streak} consecutive polls"
    fi
  fi

  sleep "${INTERVAL_SEC}"
done

trade_count="$(count_trades_since_start)"
if (( trade_count >= TARGET_TRADES )); then
  log "trade_soak_monitor complete — ${trade_count} trade(s) at deadline"
  exit 0
fi
log "trade_soak_monitor complete — ${trade_count}/${TARGET_TRADES} trades in ${DURATION_SEC}s window"
exit 1
