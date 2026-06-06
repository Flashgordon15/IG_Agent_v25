#!/usr/bin/env bash
# IG Agent v25 Watchdog — auto-restarts the agent if it dies
# Runs as a background process independent of the agent itself.
# Checks every 30 s; restarts on death; caps at 10 restarts/hour.

set -uo pipefail

AGENT_DIR="/Users/chrisgordon/Desktop/IG_Agent_v25"
LOCK_FILE="$AGENT_DIR/src/data/.ig_agent_v25.lock"
LOG="$AGENT_DIR/src/data/logs/watchdog.log"
RESTART_LOG="$AGENT_DIR/src/data/logs/agent_restart.log"
MAX_RESTARTS_PER_HOUR=10
PORT=8080
CHECK_INTERVAL=30

mkdir -p "$AGENT_DIR/src/data/logs"

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

# Trap SIGTERM for clean shutdown
trap 'log "WATCHDOG: received SIGTERM — exiting cleanly"; exit 0' TERM
trap 'log "WATCHDOG: received SIGINT — exiting cleanly"; exit 0' INT

# ------------------------------------------------------------------
# agent_alive: true if port 8080 is bound AND the lock file exists
# ------------------------------------------------------------------
agent_alive() {
    lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 && [ -f "$LOCK_FILE" ]
}

# ------------------------------------------------------------------
# cleanup_stale: kill any zombie on port 8080, remove stale lock
# ------------------------------------------------------------------
cleanup_stale() {
    log "WATCHDOG: cleaning up stale resources on port $PORT"

    # Kill any process still bound to 8080 (the dead/zombie agent)
    local stale_pids
    stale_pids=$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$stale_pids" ]; then
        log "WATCHDOG: killing stale PID(s) on port $PORT: $stale_pids"
        echo "$stale_pids" | xargs kill -9 2>/dev/null || true
    fi

    # Remove stale lock file
    if [ -f "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE"
        log "WATCHDOG: removed stale lock file"
    fi
}

# ------------------------------------------------------------------
# restart_agent: wait 5 s then relaunch via nohup
# ------------------------------------------------------------------
restart_agent() {
    log "WATCHDOG: waiting 5s before restart..."
    sleep 5

    cd "$AGENT_DIR" || { log "WATCHDOG: ERROR — cannot cd to $AGENT_DIR"; return 1; }

    # Find python3 (try venv first)
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

    log "WATCHDOG: restarting agent — python=$PY"
    PYTHONPATH=src nohup "$PY" src/main.py >> "$RESTART_LOG" 2>&1 &
    local new_pid=$!
    disown "$new_pid" 2>/dev/null || true
    log "WATCHDOG: agent restarted — new PID=$new_pid"
}

# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------
log "=== WATCHDOG started pid=$$ port=$PORT interval=${CHECK_INTERVAL}s max_restarts_per_hour=$MAX_RESTARTS_PER_HOUR ==="

# Sliding window: timestamps of restarts in the last 60 minutes
declare -a restart_times=()

while true; do
    if agent_alive; then
        log "WATCHDOG: agent alive on port $PORT (lock present)"
    else
        log "WATCHDOG: agent DOWN — port $PORT not bound or lock missing"

        # Prune restart timestamps older than 60 minutes
        local_now=$(date +%s)
        fresh_times=()
        for t in "${restart_times[@]+"${restart_times[@]}"}"; do
            if (( local_now - t < 3600 )); then
                fresh_times+=("$t")
            fi
        done
        restart_times=("${fresh_times[@]+"${fresh_times[@]}"}")

        # Check restart cap
        if (( ${#restart_times[@]} >= MAX_RESTARTS_PER_HOUR )); then
            log "WATCHDOG: FATAL — $MAX_RESTARTS_PER_HOUR restarts in the last 60 minutes; something is fundamentally broken. Watchdog STOPPING to prevent restart storm."
            exit 1
        fi

        # Clean up and restart
        cleanup_stale
        restart_agent

        # Record this restart
        restart_times+=("$(date +%s)")
        log "WATCHDOG: restart #${#restart_times[@]} of $MAX_RESTARTS_PER_HOUR allowed per hour"
    fi

    sleep "$CHECK_INTERVAL"
done
