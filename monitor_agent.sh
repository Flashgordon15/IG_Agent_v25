#!/bin/bash
# IG Agent v29 — external health watchdog
# Run this in a separate terminal: bash monitor_agent.sh
# Pings /health every 60s; if 3 consecutive failures, restarts the agent.

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
HEALTH_URL="http://localhost:8080/health"
LOCK_FILE="${PROJECT_ROOT}/src/data/.ig_agent_v29.lock"
LOG="${PROJECT_ROOT}/src/data/logs/watchdog.log"
FAIL_COUNT=0
MAX_FAILS=3
CHECK_INTERVAL=60

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$LOG"; }

log "=== IG Agent v29 watchdog started (check every ${CHECK_INTERVAL}s, restart after ${MAX_FAILS} fails) ==="

while true; do
    if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        if [ "$FAIL_COUNT" -gt 0 ]; then
            log "Agent recovered after ${FAIL_COUNT} failed check(s)"
        fi
        FAIL_COUNT=0
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        log "Health check FAILED ($FAIL_COUNT/$MAX_FAILS)"

        if [ "$FAIL_COUNT" -ge "$MAX_FAILS" ]; then
            log "Agent unresponsive — initiating restart"
            FAIL_COUNT=0

            # Kill existing process via lock file
            if [ -f "$LOCK_FILE" ]; then
                LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
                if [ -n "$LOCK_PID" ]; then
                    kill "$LOCK_PID" 2>/dev/null || true
                    sleep 2
                fi
            fi
            pkill -f "python.*main\.py" 2>/dev/null || true
            sleep 2
            rm -f "$LOCK_FILE"

            # Restart
            cd "$PROJECT_ROOT" || exit 1
            export IG_AGENT_ROOT="$PROJECT_ROOT"
            export PYTHONPATH="${PROJECT_ROOT}/src"
            export IG_AGENT_FROM_LAUNCHER=1
            caffeinate -i -s /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
                src/main.py >> src/data/logs/ig_agent.log 2>&1 &
            log "Agent restarted (PID=$!)"
        fi
    fi
    sleep "$CHECK_INTERVAL"
done
