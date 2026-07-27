#!/usr/bin/env bash
set -euo pipefail
ROOT="/Users/chrisgordon/Projects/IG_Agent_v25"
REPORT_MD="$ROOT/src/data/v31-production/reports/trading_report_2026-07-24_0800.md"
REPORT_JSON="$ROOT/src/data/v31-production/reports/trading_report_2026-07-24_0800.json"
DELIVERY="$ROOT/src/data/v31-production/reports/trading_report_2026-07-24_0800.delivery.txt"
PID_FILE="$ROOT/src/data/v31-production/state/overnight_desk_monitor.pid"
LOG="$ROOT/src/data/v31-production/logs/overnight_monitor_waiter.log"
log(){ echo "$(TZ=Europe/London date '+%Y-%m-%d %H:%M:%S %Z') | $*" | tee -a "$LOG"; }
log "waiter start (clean); target=$REPORT_MD"
python3 - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
import time
tz = ZoneInfo("Europe/London")
target = datetime(2026, 7, 24, 8, 0, 15, tzinfo=tz)
now = datetime.now(tz)
delay = (target - now).total_seconds()
print(f"sleeping {delay:.0f}s until {target.isoformat()}", flush=True)
if delay > 0:
    time.sleep(delay)
PY
log "awake; waiting for report file"
for i in $(seq 1 60); do
  if [[ -s "$REPORT_MD" ]]; then
    log "report present after ${i} checks"
    break
  fi
  MPID=$(tr -dc '0-9' <"$PID_FILE" 2>/dev/null | head -c 12 || true)
  if [[ -n "${MPID:-}" ]] && ! kill -0 "$MPID" 2>/dev/null; then
    log "WARN monitor pid $MPID not running; still waiting for report"
  fi
  sleep 30
done
if [[ ! -s "$REPORT_MD" ]]; then
  log "ERROR report missing — emergency --report-only"
  cd "$ROOT"
  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json PYTHONPATH=src \
    .venv/bin/python3 -u scripts/overnight_desk_monitor.py --report-only >>"$LOG" 2>&1 || true
fi
python3 - <<'PY'
import json
from pathlib import Path
md_path = Path("/Users/chrisgordon/Projects/IG_Agent_v25/src/data/v31-production/reports/trading_report_2026-07-24_0800.md")
js_path = Path("/Users/chrisgordon/Projects/IG_Agent_v25/src/data/v31-production/reports/trading_report_2026-07-24_0800.json")
delivery = Path("/Users/chrisgordon/Projects/IG_Agent_v25/src/data/v31-production/reports/trading_report_2026-07-24_0800.delivery.txt")
text = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
data = json.loads(js_path.read_text(encoding="utf-8")) if js_path.is_file() else {}
ex = data.get("executive") or {}
lines = [
    "OVERNIGHT DUAL-DESK REPORT DELIVERY",
    f"md: {md_path}",
    f"json: {js_path}",
    f"verdict: {ex.get('verdict')}",
    f"ML operating: {ex.get('ml_operating')}",
    f"improving: {ex.get('improving')}",
    f"overnight net £: {ex.get('overnight_net_gbp')}",
    f"overnight trades: {ex.get('overnight_trades')}",
    f"ready_for_day: {ex.get('ready_for_day')}",
    "",
    "=== markdown executive excerpt ===",
]
if text:
    lines.extend(text.splitlines()[:45])
delivery.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("wrote", delivery)
PY
log "delivery written $DELIVERY"
