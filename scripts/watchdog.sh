#!/usr/bin/env bash
# IG Agent watchdog — auto-restarts the agent if it dies.
# Port and lock path are read from $HOME/.ig_agent_global/active_lock_pointer
# (written by RuntimeIdentity.export_pointer_for_scripts on every boot).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/detach_exec.sh
source "${SCRIPT_DIR}/lib/detach_exec.sh"
if [ -n "${IG_AGENT_ROOT:-}" ]; then
    AGENT_DIR="${IG_AGENT_ROOT}"
else
    AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

POINTER_FILE="${HOME}/.ig_agent_global/active_lock_pointer"
LOG="$AGENT_DIR/src/data/logs/watchdog.log"
RESTART_LOG="$AGENT_DIR/src/data/logs/agent_restart.log"
PID_FILE="$AGENT_DIR/src/data/watchdog.pid"
START_SCRIPT="$AGENT_DIR/scripts/start_agent_background.sh"
MAX_RESTARTS_PER_HOUR=10
CHECK_INTERVAL=30
STARTUP_GRACE_SEC=720
# A live main.py that has not bound the API port within this window is a hung
# boot (Gate2 hydration stall etc). Reap it and force a clean restart instead of
# deferring on "booting" indefinitely — the trap behind the 76-minute outage.
BOOT_HANG_SEC=300
boot_first_seen=0

# Resolved each cycle from the global lock pointer (no hardcoded port/lock).
LOCK_FILE=""
PORT="8080"

mkdir -p "$AGENT_DIR/src/data/logs"

resolve_lock_file() {
    if [ -f "$POINTER_FILE" ]; then
        local p
        p="$(tr -d '[:space:]' < "$POINTER_FILE" 2>/dev/null || true)"
        if [ -n "$p" ]; then
            printf '%s\n' "$p"
            return 0
        fi
    fi
    printf '%s\n' "${AGENT_DIR}/src/data/.ig_agent_v30_port_8080.lock"
}

resolve_port_from_lock() {
    local lock="$1"
    local base port
    base="$(basename "$lock")"
    port="${base#.ig_agent_v30_port_}"
    port="${port%.lock}"
    if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -gt 0 ]; then
        printf '%s\n' "$port"
        return 0
    fi
    printf '%s\n' "8080"
}

resolve_agent_dir_from_lock() {
    local lock="$1"
    if [[ "$lock" == */src/data/* ]]; then
        printf '%s\n' "${lock%/src/data/*}"
        return 0
    fi
    printf '%s\n' "$AGENT_DIR"
}

resolve_runtime_targets() {
    LOCK_FILE="$(resolve_lock_file)"
    PORT="$(resolve_port_from_lock "$LOCK_FILE")"
    AGENT_DIR="$(resolve_agent_dir_from_lock "$LOCK_FILE")"
    LOG="$AGENT_DIR/src/data/logs/watchdog.log"
    RESTART_LOG="$AGENT_DIR/src/data/logs/agent_restart.log"
    PID_FILE="$AGENT_DIR/src/data/watchdog.pid"
    START_SCRIPT="$AGENT_DIR/scripts/start_agent_background.sh"
    START_LAUNCHD="$AGENT_DIR/scripts/start_agent_launchd.py"
    mkdir -p "$AGENT_DIR/src/data/logs"
}

legacy_watchdog_paused() {
    local marker="${AGENT_DIR}/src/data/v31-production/state/v32_legacy_watchdog_paused.json"
    [[ -f "$marker" ]]
}

v32_dual_port_active() {
    if [[ "${IG_V32_DUAL_PORT:-}" == "1" ]]; then
        return 0
    fi
    local marker="${AGENT_DIR}/src/data/v31-production/state/v32_dual_supervision.json"
    if [[ -f "$marker" ]]; then
        return 0
    fi
    if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1 \
        && lsof -iTCP:8081 -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

v32_dual_ports_healthy() {
    local cfd sb
    cfd="$(curl -sf --max-time 2 "http://127.0.0.1:8080/api/health" 2>/dev/null || true)"
    sb="$(curl -sf --max-time 2 "http://127.0.0.1:8081/api/health" 2>/dev/null || true)"
    [[ -n "$cfd" && -n "$sb" ]]
}

watchdog_already_running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    local old_pid
    old_pid="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
    if [ -z "$old_pid" ] || [ "$old_pid" = "$$" ]; then
        return 1
    fi
    kill -0 "$old_pid" 2>/dev/null
}

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

trap 'rm -f "$PID_FILE"; log "WATCHDOG: received SIGTERM — exiting cleanly"; exit 0' TERM
trap 'rm -f "$PID_FILE"; log "WATCHDOG: received SIGINT — exiting cleanly"; exit 0' INT

resolve_runtime_targets

if legacy_watchdog_paused && [[ "${IG_V32_DUAL_PORT:-}" != "1" ]]; then
    log "WATCHDOG: v32 legacy pause marker active — exiting (dual desk owns supervision)"
    exit 0
fi

if watchdog_already_running; then
    log "WATCHDOG: already running pid=$(tr -d '[:space:]' < "$PID_FILE") — exiting duplicate"
    exit 0
fi

echo "$$" > "$PID_FILE"

# All main.py PIDs for THIS project — matches both absolute invocations
# ("$AGENT_DIR/src/main.py") and relative ones ("python3 -u src/main.py" spawned
# by daemon_supervisor with cwd=$AGENT_DIR). Relative matches are cwd-verified so
# agents from other checkouts are never claimed.
agent_main_pids() {
    local abs rel pid cwd
    abs="$(/usr/bin/pgrep -f "${AGENT_DIR}/src/main.py" 2>/dev/null || true)"
    rel=""
    for pid in $(/usr/bin/pgrep -f "[s]rc/main\.py" 2>/dev/null || true); do
        case " ${abs} " in *" ${pid} "*) continue ;; esac
        cwd="$(lsof -a -p "${pid}" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
        if [ -n "${cwd}" ] && [ "${cwd}" = "${AGENT_DIR}" ]; then
            rel="${rel} ${pid}"
        fi
    done
    printf '%s %s\n' "${abs}" "${rel}" | tr ' ' '\n' | sed '/^$/d'
}

agent_alive() {
    lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || return 1
    [ -f "$LOCK_FILE" ] && return 0
    # The instance lock is written after the API binds — during that window a
    # healthy agent answers /api/health_light. Never treat it as a zombie.
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
        "http://127.0.0.1:${PORT}/api/health_light" 2>/dev/null || true)"
    case "$code" in
        200|503) return 0 ;;
    esac
    return 1
}

main_py_booting() {
    # A live project-scoped main.py process IS the boot signal — the port bind
    # and lock write land later in the boot choreography. Hung boots are still
    # bounded by the restart cap + trading_healthy checks once the port binds.
    local pids
    pids="$(agent_main_pids)"
    [ -n "${pids}" ]
}

clear_stale_session_lock() {
    # The instance/port lock ($LOCK_FILE) is only half the story. main.py also
    # holds an account-scoped SESSION lock (src/data/<profile>/session_ig_*.lock).
    # When the agent dies leaving a stale session lock, every restart collides on
    # it and the boot aborts — the classic restart-fail loop. Clear it here using
    # the same helper main.py uses (only removes locks held by dead/zombie PIDs).
    local PY
    PY="$(resolve_python_bin)"
    if [ -z "${PY}" ]; then
        return 0
    fi
    APP_MODE="${APP_MODE:-DEMO}" \
    IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}" \
    PYTHONPATH="${AGENT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PY}" - <<'PYEOF' >> "$LOG" 2>&1 || true
import sys
from pathlib import Path

try:
    from runtime.app_mode import resolve_app_mode, resolve_data_root
    from runtime.session_lock import (
        clear_stale_lock,
        lock_path_for_scope,
        resolve_account_scope,
    )

    mode = resolve_app_mode()
    scope = resolve_account_scope(mode)
    root = Path(resolve_data_root(mode))
    path = lock_path_for_scope(scope, root)
    if clear_stale_lock(path):
        print(f"WATCHDOG: cleared stale session lock ({path})")
except Exception as exc:  # never block restart on cleanup failure
    print(f"WATCHDOG: session lock clear skipped: {type(exc).__name__}: {exc}")
PYEOF
}

clear_stale_agent_lock() {
    if [ ! -f "$LOCK_FILE" ]; then
        return 0
    fi
    local lock_pid=""
    lock_pid=$(head -1 "$LOCK_FILE" 2>/dev/null | awk '{print $1}' || true)
    if [ -z "$lock_pid" ]; then
        rm -f "$LOCK_FILE"
        log "WATCHDOG: removed empty instance lock ($LOCK_FILE)"
        return 0
    fi
    if kill -0 "$lock_pid" 2>/dev/null \
        && lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 0
    fi
    if ! kill -0 "$lock_pid" 2>/dev/null; then
        rm -f "$LOCK_FILE"
        log "WATCHDOG: removed stale instance lock (pid=${lock_pid} not running)"
    elif ! lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
        # Lock pid alive but port unbound — a booting agent looks exactly like
        # this before the API binds. Only reap if no project main.py is running.
        if main_py_booting; then
            log "WATCHDOG: lock pid=${lock_pid} alive and main.py booting — keeping lock"
            return 0
        fi
        rm -f "$LOCK_FILE"
        log "WATCHDOG: removed stale instance lock (pid=${lock_pid} but port ${PORT} free)"
    fi
}

clear_stale_agent_lock

manual_stop_active() {
    # Prefer Python dual-path helper (data_dir + legacy). Bash OR-fallback keeps
    # launchd safe if import fails mid-deploy.
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
    if PYTHONPATH="${AGENT_DIR}/src" "$PY" -c \
        "from system.shutdown_cleanup import manual_stop_active" \
        2>/dev/null
    then
        PYTHONPATH="${AGENT_DIR}/src" "$PY" -c \
            "import sys; from system.shutdown_cleanup import manual_stop_active as a; sys.exit(0 if a() else 1)" \
            2>/dev/null
        return $?
    fi
    # Import failed — probe both on-disk markers (OR semantics).
    local flag
    for flag in \
        "$AGENT_DIR/src/data/state/manual_stop.json" \
        "$AGENT_DIR/src/data/v31-production/state/manual_stop.json"
    do
        [ -f "$flag" ] || continue
        if "$PY" -c "
import json, sys, time
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding='utf-8'))
    age = time.time() - float(data.get('ts') or 0)
    sys.exit(0 if 0 <= age < 600 else 1)
except Exception:
    sys.exit(0)
" "$flag" 2>/dev/null
        then
            return 0
        fi
    done
    return 1
}

supervisor_managed() {
    local sup_file="${AGENT_DIR}/src/data/v31-production/supervisor.pid"
    if [[ -n "${IG_DATA_ROOT:-}" ]]; then
        sup_file="${IG_DATA_ROOT}/supervisor.pid"
    fi
    if [[ -f "${sup_file}" ]]; then
        local spid
        spid="$(tr -d '[:space:]' < "${sup_file}" 2>/dev/null || true)"
        if [[ -n "${spid}" ]] && kill -0 "${spid}" 2>/dev/null; then
            return 0
        fi
    fi
    pgrep -f "daemon_supervisor.sh" >/dev/null 2>&1
}

trading_healthy() {
    local health_json
    health_json=$(curl -sf --max-time 3 -H "User-Agent: IG-Agent-Watchdog/1" "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)
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

resolve_python_bin() {
    local candidate PY=""
    for candidate in \
        "${AGENT_DIR}/.venv/bin/python3" \
        "${AGENT_DIR}/venv/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3" \
        "$(command -v python3 2>/dev/null || true)"
    do
        if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
            PY="${candidate}"
            break
        fi
    done
    printf '%s\n' "${PY}"
}

last_restart_epoch=0
if agent_alive; then
    last_restart_epoch=$(date +%s)
    log "WATCHDOG: agent already up — startup grace ${STARTUP_GRACE_SEC}s (port=${PORT} lock=${LOCK_FILE})"
elif main_py_booting; then
    last_restart_epoch=$(date +%s)
    log "WATCHDOG: main.py booting — startup grace ${STARTUP_GRACE_SEC}s (port=${PORT})"
elif supervisor_managed; then
    last_restart_epoch=$(date +%s)
    log "WATCHDOG: daemon_supervisor active — startup grace ${STARTUP_GRACE_SEC}s (defer to supervisor)"
else
    log "WATCHDOG: agent down on watchdog start — first restart check immediate (port=${PORT})"
fi

cleanup_stale() {
    if supervisor_managed; then
        log "WATCHDOG: daemon_supervisor active — skip port kill (supervisor owns recovery)"
        return 0
    fi
    log "WATCHDOG: cleaning up stale resources on port $PORT"

    local stale_pids
    stale_pids=$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$stale_pids" ]; then
        log "WATCHDOG: killing stale PID(s) on port $PORT: $stale_pids"
        echo "$stale_pids" | xargs kill -9 2>/dev/null || true
    fi

    if [ -f "$LOCK_FILE" ] && ! main_py_booting; then
        rm -f "$LOCK_FILE"
        log "WATCHDOG: removed stale lock file ($LOCK_FILE)"
    fi

    # Also clear the account-scoped session lock — the real cause of the
    # restart-fail loop when a dead PID leaves it behind.
    if ! main_py_booting; then
        clear_stale_session_lock
    fi
}

restart_agent() {
    if main_py_booting; then
        log "WATCHDOG: main.py already running — skip duplicate restart"
        last_restart_epoch=$(date +%s)
        return 0
    fi
    log "WATCHDOG: waiting 5s before restart..."
    sleep 5

    local PY
    PY="$(resolve_python_bin)"
    if [ -z "${PY}" ]; then
        log "WATCHDOG: ERROR — no python3 executable found"
        return 1
    fi

    if [ ! -f "$START_LAUNCHD" ]; then
        log "WATCHDOG: ERROR — start launcher missing ($START_LAUNCHD)"
        return 1
    fi

    cd "$AGENT_DIR" || { log "WATCHDOG: ERROR — cannot cd to $AGENT_DIR"; return 1; }

    export IG_AGENT_ROOT="$AGENT_DIR"
    export APP_MODE="${APP_MODE:-DEMO}"
    export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
    export IG_AGENT_FROM_LAUNCHER=1
    export IG_AGENT_SKIP_DEPLOY_CHECK=1
    export PYTHONPATH="${AGENT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

    log "WATCHDOG: restarting agent via start_agent_launchd.py (python=${PY} APP_MODE=${APP_MODE})"
    detach_exec --log "$RESTART_LOG" -- "${PY}" "$START_LAUNCHD"
    local new_pid="${DETACH_PID}"
    last_restart_epoch=$(date +%s)
    log "WATCHDOG: agent restart launched — pid=$new_pid (grace ${STARTUP_GRACE_SEC}s)"
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

log "=== WATCHDOG started pid=$$ dir=$AGENT_DIR port=$PORT lock=$LOCK_FILE pointer=$POINTER_FILE interval=${CHECK_INTERVAL}s grace=${STARTUP_GRACE_SEC}s max_restarts_per_hour=$MAX_RESTARTS_PER_HOUR ==="

declare -a restart_times=()

while true; do
    resolve_runtime_targets

    if v32_dual_port_active; then
        if v32_dual_ports_healthy; then
            log "WATCHDOG: v32 dual-port mode — both :8080 and :8081 healthy; deferring single-engine restart"
            sleep "$CHECK_INTERVAL"
            continue
        fi
        log "WATCHDOG: v32 dual-port mode — twin not fully healthy; deferring legacy :${PORT} restart (use v32_runtime_start.sh)"
        sleep "$CHECK_INTERVAL"
        continue
    fi

    if supervisor_managed && ! agent_alive; then
        log "WATCHDOG: daemon_supervisor booting — deferring port cleanup/restart"
        last_restart_epoch=$(date +%s)
        sleep "$CHECK_INTERVAL"
        continue
    fi

    need_restart=0
    restart_reason=""

    if agent_alive; then
        boot_first_seen=0
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
        if main_py_booting; then
            now_epoch=$(date +%s)
            (( boot_first_seen == 0 )) && boot_first_seen=$now_epoch
            boot_elapsed=$(( now_epoch - boot_first_seen ))
            if (( boot_elapsed < BOOT_HANG_SEC )); then
                log "WATCHDOG: main.py booting (port $PORT not ready, ${boot_elapsed}s) — waiting"
                sleep "$CHECK_INTERVAL"
                continue
            fi
            log "WATCHDOG: HUNG BOOT — main.py ${boot_elapsed}s without binding port ${PORT}; reaping tree"
            notify_telegram "🚨 Watchdog: hung boot ${boot_elapsed}s — reaping and restarting (manual intervention if it recurs)"
            for p in $(agent_main_pids); do kill -TERM "$p" 2>/dev/null || true; done
            sleep 3
            for p in $(agent_main_pids); do kill -9 "$p" 2>/dev/null || true; done
            boot_first_seen=0
            need_restart=1
            restart_reason="hung boot (${boot_elapsed}s, port ${PORT} never bound)"
        elif in_startup_grace; then
            log "WATCHDOG: agent not up yet — startup grace (${STARTUP_GRACE_SEC}s)"
            sleep "$CHECK_INTERVAL"
            continue
        else
            need_restart=1
            restart_reason="agent down (port $PORT not bound or lock missing)"
        fi
    fi

    if (( need_restart )); then
        if manual_stop_active; then
            log "WATCHDOG: manual stop active — skipping auto-restart (${restart_reason})"
            sleep "$CHECK_INTERVAL"
            continue
        fi
        if supervisor_managed; then
            log "WATCHDOG: daemon_supervisor active — deferring restart (${restart_reason})"
            sleep "$CHECK_INTERVAL"
            continue
        fi
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
