#!/usr/bin/env bash
# IG Agent v25 Watchdog — auto-restarts the agent if it dies
# Runs as a background process independent of the agent itself.
# Checks every 30 s; restarts on death; caps at 10 restarts/hour.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCK_FILE="$AGENT_DIR/src/data/.ig_agent_v25.lock"
LOG="$AGENT_DIR/src/data/logs/watchdog.log"
RESTART_LOG="$AGENT_DIR/src/data/logs/agent_restart.log"
PID_FILE="$AGENT_DIR/src/data/watchdog.pid"
START_SCRIPT="$AGENT_DIR/scripts/start_agent_background.sh"
MAX_RESTARTS_PER_HOUR=10
PORT=8080
CHECK_INTERVAL=30
STARTUP_GRACE_SEC=300

mkdir -p "$AGENT_DIR/src/data/logs"

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

trap 'rm -f "$PID_FILE"; log "WATCHDOG: received SIGTERM — exiting cleanly"; exit 0' TERM
trap 'rm -f "$PID_FILE"; log "WATCHDOG: received SIGINT — exiting cleanly"; exit 0' INT

echo "$$" > "$PID_FILE"

agent_alive() {
    lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 && [ -f "$LOCK_FILE" ]
}

trading_healthy() {
    local health_json
    health_json=$(curl -sf --max-time 3 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)
    if [ -z "$health_json" ]; then
        return 1
    fi
    local PY="python3"
    for candidate in \
        "${AGENT_DIR}/.venv/bin/python3" \
        "${AGENT_DIR}/venv/bin/python3" \
        "$(command -v python3 2>/dev/null || true)"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            PY="$candidate"
            break
        fi
    done
    "$PY" -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    sys.exit(0 if d.get('trading_healthy') else 1)
except Exception:
    sys.exit(1)
" "$health_json"
}

UNHEALTHY_STREAK=0
UNHEALTHY_RESTART_AFTER=3
last_restart_epoch=$(date +%s)

cleanup_stale() {
    log "WATCHDOG: cleaning up stale resources on port $PORT"

    local stale_pids
    stale_pids=$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$stale_pids" ]; then
        log "WATCHDOG: killing stale PID(s) on port $PORT: $stale_pids"
        echo "$stale_pids" | xargs kill -9 2>/dev/null || true
    fi

    if [ -f "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE"
        log "WATCHDOG: removed stale lock file"
    fi
}

restart_agent() {
    log "WATCHDOG: waiting 5s before restart..."
    sleep 5

    if [ ! -x "$START_SCRIPT" ]; then
        log "WATCHDOG: ERROR — start script missing or not executable ($START_SCRIPT)"
        return 1
    fi

    cd "$AGENT_DIR" || { log "WATCHDOG: ERROR — cannot cd to $AGENT_DIR"; return 1; }

    log "WATCHDOG: restarting agent via start_agent_background.sh"
    nohup bash "$START_SCRIPT" >> "$RESTART_LOG" 2>&1 &
    local new_pid=$!
    disown "$new_pid" 2>/dev/null || true
    last_restart_epoch=$(date +%s)
    log "WATCHDOG: agent restart launched — shell pid=$new_pid (grace ${STARTUP_GRACE_SEC}s)"
}

notify_telegram() {
    local msg="$1"
    local PY="python3"
    for candidate in \
        "${AGENT_DIR}/.venv/bin/python3" \
        "${AGENT_DIR}/venv/bin/python3" \
        "$(command -v python3 2>/dev/null || true)"
    do
        if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
            PY="$candidate"
            break
        fi
    done
    PYTHONPATH=src "$PY" "$AGENT_DIR/scripts/telegram_alert.py" "$msg" >> "$LOG" 2>&1 || true
}

in_startup_grace() {
    local now elapsed
    now=$(date +%s)
    elapsed=$((now - last_restart_epoch))
    (( elapsed < STARTUP_GRACE_SEC ))
}

log "=== WATCHDOG started pid=$$ dir=$AGENT_DIR port=$PORT interval=${CHECK_INTERVAL}s grace=${STARTUP_GRACE_SEC}s max_restarts_per_hour=$MAX_RESTARTS_PER_HOUR ==="

declare -a restart_times=()

while true; do
    need_restart=0
    restart_reason=""

    if agent_alive; then
        if trading_healthy; then
            UNHEALTHY_STREAK=0
            log "WATCHDOG: agent alive on port $PORT (lock present, trading healthy)"
            sleep "$CHECK_INTERVAL"
            continue
        fi
        if in_startup_grace; then
            log "WATCHDOG: agent UP, warming up (startup grace ${STARTUP_GRACE_SEC}s) — skip unhealthy check"
            sleep "$CHECK_INTERVAL"
            continue
        fi
        UNHEALTHY_STREAK=$((UNHEALTHY_STREAK + 1))
        log "WATCHDOG: agent UP but trading UNHEALTHY (streak=${UNHEALTHY_STREAK}/${UNHEALTHY_RESTART_AFTER})"
        if (( UNHEALTHY_STREAK < UNHEALTHY_RESTART_AFTER )); then
            sleep "$CHECK_INTERVAL"
            continue
        fi
        need_restart=1
        restart_reason="trading zombie (unhealthy x${UNHEALTHY_STREAK})"
        UNHEALTHY_STREAK=0
    else
        if in_startup_grace; then
            log "WATCHDOG: agent not up yet — startup grace (${STARTUP_GRACE_SEC}s)"
            sleep "$CHECK_INTERVAL"
            continue
        fi
        need_restart=1
        restart_reason="agent down (port $PORT not bound or lock missing)"
    fi

    if (( need_restart )); then
        log "WATCHDOG: restart required — ${restart_reason}"

        local_now=$(date +%s)
        fresh_times=()
        for t in "${restart_times[@]+"${restart_times[@]}"}"; do
            if (( local_now - t < 3600 )); then
                fresh_times+=("$t")
            fi
        done
        restart_times=("${fresh_times[@]+"${fresh_times[@]}"}")

        if (( ${#restart_times[@]} >= MAX_RESTARTS_PER_HOUR )); then
            log "WATCHDOG: FATAL — $MAX_RESTARTS_PER_HOUR restarts in the last 60 minutes; something is fundamentally broken. Watchdog STOPPING to prevent restart storm."
            notify_telegram "🚨 Watchdog FATAL — restart cap hit, manual intervention required"
            exit 1
        fi

        cleanup_stale
        restart_agent
        restart_times+=("$(date +%s)")
        log "WATCHDOG: restart #${#restart_times[@]} of $MAX_RESTARTS_PER_HOUR allowed per hour"
        notify_telegram "🔄 Watchdog restarted agent (#${#restart_times[@]}): ${restart_reason}"
    fi

    sleep "$CHECK_INTERVAL"
done
