#!/usr/bin/env bash
# Overnight monitor — health + feeder checks every 5 min until 08:00 local.
# Log: src/data/logs/overnight_watch.log
# At 08:00–08:10 runs morning_report_v26.py → docs/morning/MORNING_REPORT_<date>.md

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/src/data/logs/overnight_watch.log"
INTERVAL=300
PORT=8080

log() { printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

health_ok() {
  curl -sf -m 10 "http://localhost:${PORT}/api/health" >/dev/null 2>&1
}

agent_pid() {
  pgrep -f "Python src/main.py" 2>/dev/null | head -1
}

feeder_lines() {
  local day
  day="$(date -u '+%Y-%m-%d')"
  local f="$ROOT/data_lake/events/${day}.jsonl"
  if [ -f "$f" ]; then wc -l < "$f" | tr -d ' '; else echo "0"; fi
}

run_morning_report() {
  log "Generating morning report..."
  cd "$ROOT" || exit 1
  PYTHONPATH=src:v26 python3 scripts/morning_report_v26.py >>"$LOG" 2>&1
  log "Morning report complete."
}

already_reported_today() {
  local out="$ROOT/docs/morning/MORNING_REPORT_$(date '+%Y-%m-%d').md"
  [ -f "$out" ] && [ -s "$out" ]
}

in_morning_window() {
  local hour min
  hour=$((10#$(date '+%H')))
  min=$((10#$(date '+%M')))
  [ "$hour" -eq 8 ] && [ "$min" -le 10 ]
}

log "OVERNIGHT WATCH started pid=$$"

while true; do
  if in_morning_window; then
    if ! already_reported_today; then
      run_morning_report
    else
      log "Morning report already exists for $(date '+%Y-%m-%d')."
    fi
    log "Morning window complete — exiting."
    exit 0
  fi

  pid="$(agent_pid || true)"
  lines="$(feeder_lines)"
  if health_ok; then
    log "OK health=up pid=${pid:-none} feeder_lines=${lines}"
  else
    log "WARN health=DOWN pid=${pid:-none} feeder_lines=${lines} (watchdog should restart)"
    if [ -z "$pid" ]; then
      log "WARN no main.py — watchdog expected to restart within 30s"
    fi
  fi

  sleep "$INTERVAL"
done
