#!/usr/bin/env bash
# v32 Multi-Process Dual-Port runtime supervisor — CFD :8080 + SB :8081
#
# Client A / Client B REST isolation (``runtime/session_registry.py``):
#   :8080  Z6BAH4  QUANT_SNIPER   → CFD engine   (IG_SESSION_REGISTRY=1)
#   :8081  Z6BAH3  MACRO_SENTINEL → SB engine    (distinct IG-ACCOUNT-ID header)
#
# CANONICAL OPS RECIPE (DEMO, flat book only):
#   # Single CFD (preferred day-to-day — Trading_Desk.app fast path):
#   CORE_DETACHED=false ./scripts/trading_desk_silent.sh
#   # or: IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \
#   #       PYTHONPATH=src python3 scripts/session_ready.py --start-agent
#
#   # Dual twins (review / multi-port only — max one attempt, no restart loops):
#   CORE_DETACHED=false IG_V32_SKIP_DUAL_LAUNCHD=1 \
#     ./scripts/v32_runtime_start.sh start
#   ./scripts/v32_runtime_start.sh status
#   curl -s http://127.0.0.1:8080/api/health && curl -s http://127.0.0.1:8081/api/health
#
#   # Graceful stop (never kill -9 main.py):
#   ./scripts/v32_runtime_start.sh stop
#   # Or desk deploy when sessions closed:
#   ./scripts/desk_deploy.sh audit && ./scripts/desk_deploy.sh deploy
#
# SAFETY: Do NOT use kill -9 on main.py. Use anti-zombie protocol:
#   PYTHONPATH=src .venv/bin/python3 -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='operator_restart')"
#   kill -TERM <pid> && wait for port release
# Or: ./scripts/desk_deploy.sh audit|deploy (flat sessions only)
#
# Watchdog posture:
#   start  → bootout legacy launchd job, disable legacy plist, bootstrap com.igagent.v32.dual
#   stop   → bootout v32 dual, restore legacy plist (operator may bootstrap legacy watchdog)
#
# Usage:
#   ./scripts/v32_runtime_start.sh          # start both engines (background)
#   ./scripts/v32_runtime_start.sh status   # show PIDs / ports / supervision
#   ./scripts/v32_runtime_start.sh stop     # mark_manual_stop + SIGTERM both + restore watchdog marker

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${PYTHONPATH:-src}"
export IG_AGENT_ROOT="$ROOT"
export CORE_DETACHED="${CORE_DETACHED:-FALSE}"
export IG_V32_SKIP_DUAL_LAUNCHD="${IG_V32_SKIP_DUAL_LAUNCHD:-1}"

VENV_PY="${ROOT}/.venv/bin/python3"
if [[ -x "$VENV_PY" ]]; then
  PYTHON_BIN="$VENV_PY"
else
  PYTHON_BIN="$(command -v python3 || true)"
  [[ -n "$PYTHON_BIN" ]] || { echo "ERROR: no python3 found" >&2; exit 1; }
fi

CFD_PORT=8080
SB_PORT=8081
CFD_ACCOUNT="Z6BAH4"
SB_ACCOUNT="Z6BAH3"
CFD_ORIGIN="QUANT_SNIPER"
SB_ORIGIN="MACRO_SENTINEL"
DATA_ROOT="${ROOT}/src/data/v31-production"
CFD_STATE="${DATA_ROOT}/state_cfd"
SB_STATE="${DATA_ROOT}/state_sb"
SHARED_STATE="${DATA_ROOT}/state"
CFD_PID_FILE="${CFD_STATE}/agent.pid"
SB_PID_FILE="${SB_STATE}/agent.pid"
CFD_LOG="${DATA_ROOT}/logs/v32_cfd.log"
SB_LOG="${DATA_ROOT}/logs/v32_sb.log"
V32_DUAL_MARKER="${SHARED_STATE}/v32_dual_supervision.json"
V32_LEGACY_PAUSED="${SHARED_STATE}/v32_legacy_watchdog_paused.json"
V32_DUAL_PLIST_SRC="${ROOT}/scripts/com.igagent.v32.dual.plist"
V32_DUAL_PLIST_DST="${SHARED_STATE}/com.igagent.v32.dual.plist"
LEGACY_WATCHDOG_LABEL="com.igagent.v25.watchdog"
V32_DUAL_LABEL="com.igagent.v32.dual"
LAUNCH_DOMAIN="gui/$(id -u)"
LEGACY_PLIST_INSTALLED="${HOME}/Library/LaunchAgents/${LEGACY_WATCHDOG_LABEL}.plist"
LEGACY_PLIST_DISABLED="${SHARED_STATE}/${LEGACY_WATCHDOG_LABEL}.plist.disabled"
LEGACY_PLIST_REMOVED="${LEGACY_PLIST_DISABLED}.removed"

RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'

die() { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }
warn() { echo -e "${YEL}WARN:${NC} $*" >&2; }

require_python() {
  [[ -x "$PYTHON_BIN" || -n "$(command -v "$PYTHON_BIN" 2>/dev/null)" ]] \
    || die "Missing python interpreter: $PYTHON_BIN"
  if [[ "$PYTHON_BIN" != "$VENV_PY" ]]; then
    warn "venv missing — using ${PYTHON_BIN} (expected ${VENV_PY})"
  fi
}

write_json_marker() {
  local path="$1"
  local payload="$2"
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$payload" > "$path"
}

generate_dual_plist() {
  local py_bin="${PYTHON_BIN}"
  local dst="$1"
  mkdir -p "$(dirname "$dst")"
  cat > "$dst" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${V32_DUAL_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${py_bin}</string>
    <string>${ROOT}/scripts/watchdog_launchd.py</string>
    <string>--dual-port</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>IG_AGENT_ROOT</key>
    <string>${ROOT}</string>
    <key>PYTHONPATH</key>
    <string>${ROOT}/src</string>
    <key>APP_MODE</key>
    <string>DEMO</string>
    <key>IG_AGENT_CONFIG</key>
    <string>config/config_v31_demo_throughput.json</string>
    <key>IG_V32_DUAL_PORT</key>
    <string>1</string>
    <key>IG_V32_WATCH_PORTS</key>
    <string>8080,8081</string>
    <key>CORE_DETACHED</key>
    <string>${CORE_DETACHED:-FALSE}</string>
  </dict>
  <!-- KeepAlive+RunAtLoad required: watchdog.sh is a long-running dual-port
       observer loop. With both false, bootstrap loaded the label but never
       supervised :8080/:8081. Dual mode defers single-engine restarts so it
       does not fight live twins (heal via v32_runtime_start.sh). -->
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${DATA_ROOT}/logs/watchdog_v32_dual.log</string>
  <key>StandardErrorPath</key>
  <string>${DATA_ROOT}/logs/watchdog_v32_dual.log</string>
</dict>
</plist>
EOF
  cp "$dst" "$V32_DUAL_PLIST_SRC" 2>/dev/null || true
}

launchctl_bootout_label() {
  local label="$1"
  local plist="${2:-}"
  launchctl bootout "${LAUNCH_DOMAIN}/${label}" 2>/dev/null || true
  if [[ -n "$plist" && -f "$plist" ]]; then
    launchctl bootout "${LAUNCH_DOMAIN}" "$plist" 2>/dev/null || true
    launchctl unload "$plist" 2>/dev/null || true
  fi
}

launchctl_bootstrap_plist() {
  local plist="$1"
  [[ -f "$plist" ]] || return 1
  launchctl bootstrap "${LAUNCH_DOMAIN}" "$plist" 2>/dev/null \
    || launchctl load "$plist" 2>/dev/null \
    || return 1
  return 0
}

install_legacy_watchdog_plist_from_template() {
  local dst="$1"
  local src="${ROOT}/scripts/com.igagent.v25.watchdog.plist"
  [[ -f "$src" ]] || return 1
  mkdir -p "$(dirname "$dst")"
  sed -e "s|__IG_AGENT_ROOT__|${ROOT}|g" -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
    "$src" > "$dst"
}

disable_legacy_watchdog_plist() {
  if [[ -f "$LEGACY_PLIST_INSTALLED" ]]; then
    cp -f "$LEGACY_PLIST_INSTALLED" "$LEGACY_PLIST_DISABLED"
    mv -f "$LEGACY_PLIST_INSTALLED" "$LEGACY_PLIST_REMOVED"
    echo "Legacy watchdog plist moved aside → ${LEGACY_PLIST_REMOVED}"
    return 0
  fi
  if [[ ! -f "$LEGACY_PLIST_DISABLED" ]]; then
    install_legacy_watchdog_plist_from_template "$LEGACY_PLIST_DISABLED" \
      && echo "Legacy watchdog plist snapshot saved → ${LEGACY_PLIST_DISABLED}"
  fi
  return 0
}

restore_legacy_watchdog_plist() {
  if [[ -f "$LEGACY_PLIST_REMOVED" ]]; then
    mv -f "$LEGACY_PLIST_REMOVED" "$LEGACY_PLIST_INSTALLED"
    echo "Restored legacy watchdog plist → ${LEGACY_PLIST_INSTALLED}"
    return 0
  fi
  if [[ ! -f "$LEGACY_PLIST_INSTALLED" ]]; then
    install_legacy_watchdog_plist_from_template "$LEGACY_PLIST_INSTALLED" \
      && echo "Reinstalled legacy watchdog plist → ${LEGACY_PLIST_INSTALLED}"
  fi
}

neutralize_legacy_watchdog() {
  mkdir -p "$SHARED_STATE"
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  echo "Booting out legacy watchdog (${LEGACY_WATCHDOG_LABEL})..."
  launchctl_bootout_label "$LEGACY_WATCHDOG_LABEL" "$LEGACY_PLIST_INSTALLED"
  pkill -TERM -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true
  sleep 1
  disable_legacy_watchdog_plist

  write_json_marker "$V32_LEGACY_PAUSED" "{\"paused_at\":\"${ts}\",\"legacy_label\":\"${LEGACY_WATCHDOG_LABEL}\",\"reason\":\"v32_dual_start\",\"plist_disabled\":true}"
  write_json_marker "$V32_DUAL_MARKER" "{\"dual_port\":true,\"ports\":[${CFD_PORT},${SB_PORT}],\"accounts\":[\"${CFD_ACCOUNT}\",\"${SB_ACCOUNT}\"],\"started_at\":\"${ts}\",\"legacy_watchdog_paused\":true,\"legacy_plist_disabled\":true}"
  generate_dual_plist "$V32_DUAL_PLIST_DST"

  if [[ "${IG_V32_SKIP_DUAL_LAUNCHD:-0}" == "1" ]]; then
    warn "IG_V32_SKIP_DUAL_LAUNCHD=1 (default) — skipping v32 dual launchd bootstrap (manual twin supervision)"
    return 0
  fi

  if launchctl_bootstrap_plist "$V32_DUAL_PLIST_DST"; then
    echo -e "${GRN}v32 dual watchdog bootstrapped (${V32_DUAL_LABEL})${NC}"
  else
    warn "v32 dual plist bootstrap failed — twins run unsupervised until operator fixes launchctl"
  fi
}

pause_legacy_watchdog() {
  neutralize_legacy_watchdog
}

restore_legacy_watchdog_posture() {
  echo "Booting out v32 dual watchdog (${V32_DUAL_LABEL})..."
  launchctl_bootout_label "$V32_DUAL_LABEL" "$V32_DUAL_PLIST_DST"
  pkill -TERM -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true

  rm -f "$V32_DUAL_MARKER" "$V32_LEGACY_PAUSED" 2>/dev/null || true
  restore_legacy_watchdog_plist

  echo -e "${YEL}v32 dual supervision markers cleared${NC}"
  echo "To restore legacy single-port watchdog supervision:"
  echo "  launchctl bootstrap ${LAUNCH_DOMAIN} ${LEGACY_PLIST_INSTALLED}"
  echo "  # or: ${ROOT}/scripts/install_launchd.sh"
}

dual_supervision_status_line() {
  if [[ -f "$V32_DUAL_MARKER" ]]; then
    echo -e "${GRN}v32 dual supervision marker: present${NC}"
  else
    echo -e "${YEL}v32 dual supervision marker: absent${NC}"
  fi
  if [[ -f "$V32_LEGACY_PAUSED" ]]; then
    echo -e "${YEL}legacy watchdog: PAUSED (marker + plist disabled)${NC}"
  else
    echo -e "legacy watchdog: default posture"
  fi
  if launchctl print "${LAUNCH_DOMAIN}/${LEGACY_WATCHDOG_LABEL}" >/dev/null 2>&1; then
    echo -e "${RED}legacy launchd job STILL LOADED — bootout required${NC}"
  else
    echo -e "${GRN}legacy launchd job: not loaded${NC}"
  fi
  if launchctl print "${LAUNCH_DOMAIN}/${V32_DUAL_LABEL}" >/dev/null 2>&1; then
    echo -e "${GRN}v32 dual launchd job: loaded${NC}"
  else
    echo -e "${YEL}v32 dual launchd job: not loaded${NC}"
  fi
  if [[ -f "$V32_DUAL_PLIST_DST" ]]; then
    echo "dual plist: ${V32_DUAL_PLIST_DST}"
  fi
}

purge_stale_session_locks() {
  local rc=0
  set +e
  PYTHONPATH="${PYTHONPATH}" "$PYTHON_BIN" -c \
    "from pathlib import Path; from runtime.session_lock import lock_path_for_scope, clear_stale_lock; dr=Path('${DATA_ROOT}'); [clear_stale_lock(lock_path_for_scope(s,dr)) for s in ('ig:${CFD_ACCOUNT}','ig:${SB_ACCOUNT}')]"
  rc=$?
  set -e
  if (( rc != 0 )); then
    warn "stale session lock purge exited ${rc} (continuing — live locks may remain)"
  fi
}

# Collect unique numeric PIDs listening on a TCP port (lsof + optional fuser).
_discover_port_listener_pids() {
  local port="$1"
  local -a found=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && found+=("$pid")
  done < <(lsof -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true)
  if command -v fuser >/dev/null 2>&1; then
    while IFS= read -r pid; do
      [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && found+=("$pid")
    done < <(fuser -n tcp "${port}" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)
  fi
  local -a unique=()
  local seen p u
  # set -u: empty array expansion is unbound on some bash builds
  for p in ${found[@]+"${found[@]}"}; do
    seen=0
    for u in ${unique[@]+"${unique[@]}"}; do
      [[ "$u" == "$p" ]] && seen=1 && break
    done
    (( seen == 0 )) && unique+=("$p")
  done
  if ((${#unique[@]})); then
    printf '%s\n' "${unique[@]}"
  fi
}

# Remove stale lock files under twin state dirs and data root session locks.
_clear_runtime_lock_files() {
  rm -f "${DATA_ROOT}"/session_ig_*.lock 2>/dev/null || true
  rm -f "${CFD_STATE}"/session_ig_*.lock "${SB_STATE}"/session_ig_*.lock 2>/dev/null || true
  rm -f "${CFD_STATE}/.ig_agent_"*.lock "${SB_STATE}/.ig_agent_"*.lock 2>/dev/null || true
  purge_stale_session_locks
}

# Anti-zombie port eviction for :8080 / :8081.
#
# Protocol (compatible with mark_manual_stop / desk_deploy):
#   1. Optionally mark_manual_stop when stopping (arg mark_stop=1)
#   2. Discover port-holder PIDs via lsof (+ fuser when available)
#   3. SIGTERM those PIDs and pid-file twins; wait up to EVICT_TERM_WAIT_SEC
#   4. If still listening: fuser -k on ports OR kill -9 **only** port-holder PIDs
#      — NEVER killall -9 python3; escalate only after TERM timeout
#   5. rm stale locks (state_cfd, state_sb, session_ig_*.lock) + clear_stale_lock
#
# Usage: evict_port_holders [mark_stop=0|1] [term_wait_sec=18]
evict_port_holders() {
  local mark_stop="${1:-0}"
  local term_wait="${2:-18}"
  local -a port_pids=()
  local port pid

  if [[ "$mark_stop" == "1" ]]; then
    echo "Engaging manual stop hold (evict_port_holders)..."
    PYTHONPATH="${PYTHONPATH}" "$PYTHON_BIN" -c \
      "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='v32_runtime_stop')" \
      || true
  fi

  for port in "$CFD_PORT" "$SB_PORT"; do
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && port_pids+=("$pid")
    done < <(_discover_port_listener_pids "$port")
  done

  for pid_file in "$CFD_PID_FILE" "$SB_PID_FILE"; do
    pid="$(read_pid_file "$pid_file")"
    if pid_alive "$pid"; then
      port_pids+=("$pid")
    fi
  done

  local -a unique_pids=()
  local seen p u
  for p in "${port_pids[@]:-}"; do
    seen=0
    for u in "${unique_pids[@]:-}"; do
      [[ "$u" == "$p" ]] && seen=1 && break
    done
    (( seen == 0 )) && unique_pids+=("$p")
  done

  if ((${#unique_pids[@]})); then
    echo "SIGTERM port-holders: ${unique_pids[*]}"
    for pid in "${unique_pids[@]}"; do
      kill -TERM "$pid" 2>/dev/null || true
    done
  else
    echo "No port-holders discovered on :${CFD_PORT}/:${SB_PORT}"
  fi

  local deadline=$((SECONDS + term_wait))
  while (( SECONDS < deadline )); do
    port_in_use "$CFD_PORT" || port_in_use "$SB_PORT" || break
    sleep 1
  done

  if command -v fuser >/dev/null 2>&1; then
    if port_in_use "$CFD_PORT" || port_in_use "$SB_PORT"; then
      warn "Aggressive fuser sweep on :${CFD_PORT}/:${SB_PORT} (port-holders only)"
      fuser -k "${CFD_PORT}"/tcp "${SB_PORT}"/tcp 2>/dev/null || true
      sleep 1
    fi
  fi

  for port in "$CFD_PORT" "$SB_PORT"; do
    if ! port_in_use "$port"; then
      continue
    fi
    warn "Port :${port} still bound after ${term_wait}s TERM — escalating (port-holders only; never killall python3)"
    if command -v fuser >/dev/null 2>&1; then
      fuser -k "${port}"/tcp 2>/dev/null || true
    fi
    while IFS= read -r pid; do
      [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -9 "$pid" 2>/dev/null || true
    done < <(_discover_port_listener_pids "$port")
    sleep 1
  done

  # Final verification sweep — kill -9 any stubborn listeners (never killall python3).
  for port in "$CFD_PORT" "$SB_PORT"; do
    if ! port_in_use "$port"; then
      continue
    fi
    warn "Port :${port} still bound post-fuser — final kill -9 on discovered holders"
    while IFS= read -r pid; do
      [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -9 "$pid" 2>/dev/null || true
    done < <(_discover_port_listener_pids "$port")
    sleep 1
  done

  _clear_runtime_lock_files
}

# Drop stale trade_support SoT cache from prior session — prevents boot splash
# GATE HOLD / STABILITY R from ~20s stale status before wrapper's first poll.
_evict_stale_trade_support_cache() {
  echo "Evicting stale trade_support SoT cache before twin spawn..."
  rm -f "${DATA_ROOT}/trade_support_status.json" 2>/dev/null || true
  rm -f "${DATA_ROOT}"/trade_support_sot.* 2>/dev/null || true
  rm -f "${DATA_ROOT}"/trade_support_* 2>/dev/null || true
  rm -f "${CFD_STATE}"/trade_support_sot.* "${SB_STATE}"/trade_support_sot.* 2>/dev/null || true
  rm -f "${SHARED_STATE}"/trade_support_sot.* 2>/dev/null || true
  rm -f "${ROOT}/src/data/trade_support_status.json" 2>/dev/null || true
  rm -f "${ROOT}/src/data"/trade_support_sot.* 2>/dev/null || true
}

# Forceful environmental reset upon start — clean baseline before twin spawn.
# User-requested fuser -k + lock flush. On Darwin, fuser may be limited (|| true);
# evict_port_holders still provides lsof TERM path. Never deletes manual_stop,
# learning DB, journals, or operator deploy holds.
forceful_environmental_reset() {
  echo "Forceful environmental reset — flush residual locks/tokens..."
  # Prefer DATA_ROOT-relative paths (IG_DATA_ROOT / v31-production).
  fuser -k "${CFD_PORT}/tcp" "${SB_PORT}/tcp" 2>/dev/null || true
  rm -f "${CFD_STATE}"/*.lock "${SB_STATE}"/*.lock "${DATA_ROOT}"/*.lock 2>/dev/null || true
  _clear_runtime_lock_files
  PYTHONPATH="${PYTHONPATH}" "$PYTHON_BIN" -c "
from system.boot.env_reset import forceful_environmental_reset
import json
print(json.dumps(forceful_environmental_reset('${DATA_ROOT}', clear_sot_cache=False)))
" 2>/dev/null || true
}

# Token-paced hydration shifter: CFD must authenticate + hydrate SHM completely
# BEFORE SB fires. Why stagger exists: rest_pressure / init burst isolation —
# concurrent twin spawn on rest_poll bursts IG auth + OHLC + SHM bind and can
# drive REST_PRESSURE_HIGH / twin death. Prefer readiness gates over blind sleep;
# after CFD ready, enforce ≥4s post-ready window before SB spawn.
wait_cfd_ready_then_stagger_sb() {
  local ready_timeout="${IG_V32_CFD_READY_TIMEOUT_SEC:-180}"
  local post_ready="${IG_V32_SB_POST_READY_STAGGER_SEC:-4}"
  # Enforce minimum 4s isolation window (rest_pressure / init burst isolation).
  if (( post_ready < 4 )); then
    post_ready=4
  fi
  echo "Waiting for CFD :${CFD_PORT} healthy/hydrated (timeout ${ready_timeout}s) before SB stagger..."
  local plan
  plan="$(
    PYTHONPATH="${PYTHONPATH}" IG_V32_CFD_READY_TIMEOUT_SEC="${ready_timeout}" \
      IG_V32_SB_POST_READY_STAGGER_SEC="${post_ready}" \
      "$PYTHON_BIN" -c "
from system.boot.dual_desk_stagger import wait_cfd_ready_then_stagger
import json, sys
r = wait_cfd_ready_then_stagger(
    port=int('${CFD_PORT}'),
    ready_timeout_sec=float('${ready_timeout}'),
    min_post_ready_sec=float('${post_ready}'),
)
print(json.dumps(r))
sys.exit(0 if r.get('sb_spawn_allowed') else 1)
"
  )" || {
    warn "CFD readiness gate timed out or failed — ${plan:-no plan}"
    warn "Falling back to ${post_ready}s post-ready stagger only (rest_pressure isolation)"
    sleep "${post_ready}"
    return 0
  }
  echo "CFD ready + ${post_ready}s post-ready stagger complete (rest_pressure / init burst isolation)"
  echo "stagger_plan=${plan}"
}

port_in_use() {
  local port="$1"
  lsof -iTCP:"${port}" -sTCP:LISTEN -t >/dev/null 2>&1
}

port_holder_pid() {
  local port="$1"
  lsof -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

read_pid_file() {
  local f="$1"
  if [[ -f "$f" ]]; then
    tr -d '[:space:]' < "$f"
  fi
}

pid_alive() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

check_flat_book() {
  local flat
  flat="$(curl -sf "http://127.0.0.1:${CFD_PORT}/api/positions/live" 2>/dev/null | "$PYTHON_BIN" -c "import sys,json; d=json.load(sys.stdin); print('flat' if d.get('verdict')=='FLAT' and int(d.get('count',0))==0 else 'open')" 2>/dev/null || echo "unknown")"
  if [[ "$flat" == "open" ]]; then
    warn "Broker may have open risk on :${CFD_PORT} — review before dual start"
  fi
}

preflight_ports() {
  local p
  for p in "$CFD_PORT" "$SB_PORT"; do
    if port_in_use "$p"; then
      return 1
    fi
  done
  return 0
}

ensure_dirs() {
  mkdir -p "$CFD_STATE" "$SB_STATE" "$SHARED_STATE" "${DATA_ROOT}/logs"
}

launch_engine() {
  local port="$1" account="$2" origin="$3" pid_file="$4" log_file="$5" label="$6"
  # Per-twin SHM isolation: lane+port names (cfd_8080 / sb_8081) — account fallback in resolvers.
  local shm_lane="eng_${port}"
  if [[ "$port" == "$CFD_PORT" ]]; then
    shm_lane="cfd_${CFD_PORT}"
  elif [[ "$port" == "$SB_PORT" ]]; then
    shm_lane="sb_${SB_PORT}"
  fi
  local shm_name="ig_agent_v33_shm_${shm_lane}"
  local cockpit_shm_name="ig_agent_v33_cockpit_${shm_lane}"
  local -a launch_env=(
    APP_MODE="$APP_MODE"
    IG_AGENT_CONFIG="$IG_AGENT_CONFIG"
    PYTHONPATH="$PYTHONPATH"
    CORE_DETACHED="$CORE_DETACHED"
    IG_BARE_METAL_EXEC=1
    IG_V32_DUAL_PORT=1
    IG_SESSION_REGISTRY=1
    IG_API_PORT="$port"
    PORT="$port"
    IG_ACCOUNT_ID="$account"
    IG_ACCOUNT_SCOPE="ig:${account}"
    IG_ENGINE_ORIGIN="$origin"
    IG_SHM_RING_NAME="$shm_name"
    IG_SHM_RING_CREATE=1
    IG_COCKPIT_SHM_NAME="$cockpit_shm_name"
  )
  if [[ "$port" == "$CFD_PORT" ]]; then
    launch_env+=(IG_AGENT_ORCHESTRATOR=1)
  fi
  # shellcheck source=lib/detach_exec.sh
  source "${ROOT}/scripts/lib/detach_exec.sh"
  detach_exec --log "$log_file" -- env "${launch_env[@]}" \
    "$PYTHON_BIN" src/main.py \
    --port="$port" \
    --account-id="$account" \
    --origin="$origin"
  local pid="${DETACH_PID}"
  # Write spawn pid immediately, then reconcile to the actual LISTEN holder
  # after bind (avoids stale state_sb/agent.pid when heal/relaunch races).
  echo "$pid" > "$pid_file"
  local listener="" attempt=0
  while (( attempt < 40 )); do
    listener="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
    if [[ -n "$listener" ]]; then
      if [[ "$listener" != "$pid" ]]; then
        echo -e "${YEL}${label}: pid file reconciled ${pid} → listener ${listener} on :${port}${NC}" >&2
      fi
      echo "$listener" > "$pid_file"
      pid="$listener"
      break
    fi
    # Spawn died before bind — leave spawn pid for status diagnostics.
    if ! kill -0 "$pid" 2>/dev/null; then
      echo -e "${YEL}${label}: spawn pid ${pid} exited before :${port} listen${NC}" >&2
      break
    fi
    sleep 0.25
    attempt=$((attempt + 1))
  done
  echo "$pid"
}

start_both() {
  require_python
  ensure_dirs
  # Durable interim offline — never auto/manual start twins while hold is set.
  if [[ -f "${DATA_ROOT}/state/desk_offline_hold.json" ]] \
    || [[ -f "${ROOT}/src/data/state/desk_offline_hold.json" ]]; then
    die "desk_offline_hold active — refusing start. Clear with: PYTHONPATH=src .venv/bin/python3 -c \"from runtime.desk_offline_hold import clear_desk_offline_hold; print(clear_desk_offline_hold())\""
  fi
  check_flat_book
  # Forceful env reset FIRST — fuser -k + lock flush for clean baseline.
  forceful_environmental_reset
  echo "Evicting stale port-holders before twin spawn (lsof TERM + Darwin-safe path)..."
  evict_port_holders 0
  _evict_stale_trade_support_cache
  if ! preflight_ports; then
    die "Ports :${CFD_PORT}/:${SB_PORT} still bound after eviction — resolve manually (desk_deploy runbook)"
  fi
  # Clear prior stop/hold flags so twin boot is not blocked by v32 stop or orchestrator port_offline hold.
  # Never clears desk_offline_hold (explicit operator reopen required).
  PYTHONPATH="${PYTHONPATH}" "$PYTHON_BIN" -c "from system.shutdown_cleanup import clear_manual_stop; clear_manual_stop()" 2>/dev/null || true
  PYTHONPATH="${PYTHONPATH}" "$PYTHON_BIN" -c "
from system.startup_hold_clear import clear_stale_entry_holds_if_flat
r = clear_stale_entry_holds_if_flat(port=${CFD_PORT}, reason='v32_start', allow_offline_stale_clear=True)
print('startup_hold_clear', r)
" 2>/dev/null || true
  pause_legacy_watchdog

  echo "Stopping stray standalone watchdog.sh processes (dual-port uses v32 supervision)..."
  pkill -TERM -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true
  sleep 1

  echo "Starting v32 dual-port engines (token-paced: CFD hydrate → ≥4s → SB)..."
  _core_detached_upper="$(printf '%s' "${CORE_DETACHED:-FALSE}" | tr '[:lower:]' '[:upper:]')"
  if [[ "$_core_detached_upper" == "TRUE" ]]; then
    echo -e "${YEL}CORE_DETACHED=TRUE — maintenance detachment active (no broker orders)${NC}"
    echo "Dry-run safety: CORE_DETACHED=TRUE ./scripts/v32_runtime_start.sh start"
  fi
  local cfd_pid sb_pid
  # Process 1 — CFD Sniper :8080 must spawn, authenticate, and hydrate SHM
  # completely BEFORE Process 2 SB Sentinel :8081 fires.
  cfd_pid="$(launch_engine "$CFD_PORT" "$CFD_ACCOUNT" "$CFD_ORIGIN" "$CFD_PID_FILE" "$CFD_LOG" "CFD")"
  wait_cfd_ready_then_stagger_sb
  sb_pid="$(launch_engine "$SB_PORT" "$SB_ACCOUNT" "$SB_ORIGIN" "$SB_PID_FILE" "$SB_LOG" "SB")"

  sleep 2
  print_status_block "$cfd_pid" "$sb_pid"
}

stop_both() {
  require_python
  evict_port_holders 1
  restore_legacy_watchdog_posture
  echo "Stop sequence complete."
}

_engine_status_cell() {
  local pid_file="$1" port="$2"
  local pid listen alive holder
  pid="$(read_pid_file "$pid_file")"
  if pid_alive "$pid"; then
    alive="yes"
  else
    alive="no"
  fi
  if port_in_use "$port"; then
    listen="yes"
    holder="$(port_holder_pid "$port")"
  else
    listen="no"
    holder="—"
  fi
  printf '%s|%s|%s|%s' "${pid:-—}" "$alive" "$listen" "${holder:-—}"
}

print_status_block() {
  local cfd_pid="${1:-$(read_pid_file "$CFD_PID_FILE")}"
  local sb_pid="${2:-$(read_pid_file "$SB_PID_FILE")}"
  local cfd_cell sb_cell
  cfd_cell="$(_engine_status_cell "$CFD_PID_FILE" "$CFD_PORT")"
  sb_cell="$(_engine_status_cell "$SB_PID_FILE" "$SB_PORT")"
  IFS='|' read -r cfd_pid_file cfd_alive cfd_listen cfd_holder <<< "$cfd_cell"
  IFS='|' read -r sb_pid_file sb_alive sb_listen sb_holder <<< "$sb_cell"

  cat <<EOF

+--------------------------------------------------------------------------------+
| v32 DUAL-PORT RUNTIME                                                          |
+--------------------------------------------------------------------------------+
| ENGINE       | PORT | ACCOUNT | ORIGIN         | PID    | ALIVE | LISTEN | HOLDER |
|--------------|------|---------|----------------|--------|-------|--------|--------|
| CFD Sniper   | ${CFD_PORT} | ${CFD_ACCOUNT} | ${CFD_ORIGIN} | $(printf '%-6s' "${cfd_pid_file}") | $(printf '%-5s' "${cfd_alive}") | $(printf '%-6s' "${cfd_listen}") | $(printf '%-6s' "${cfd_holder}") |
| SB Sentinel  | ${SB_PORT} | ${SB_ACCOUNT} | ${SB_ORIGIN} | $(printf '%-6s' "${sb_pid_file}") | $(printf '%-5s' "${sb_alive}") | $(printf '%-6s' "${sb_listen}") | $(printf '%-6s' "${sb_holder}") |
+--------------------------------------------------------------------------------+
| SHM ring:    ig_agent_v33_shm_cfd_${CFD_PORT} (:${CFD_PORT}) | ig_agent_v33_shm_sb_${SB_PORT} (:${SB_PORT})
| SHM cockpit: ig_agent_v33_cockpit_cfd_${CFD_PORT}             | ig_agent_v33_cockpit_sb_${SB_PORT}
| Data root:   ${DATA_ROOT}
| State dirs:  ${CFD_STATE}/  |  ${SB_STATE}/
| Shared:      ${DATA_ROOT}/state/ (deploy_hold, rest_budget, dual markers)
+--------------------------------------------------------------------------------+
EOF
  if [[ "$cfd_listen" == "yes" && "$cfd_alive" == "yes" ]]; then
    echo -e "${GRN}CFD :${CFD_PORT} healthy (pid=${cfd_pid_file})${NC}"
  elif [[ "$cfd_listen" == "yes" ]]; then
    echo -e "${YEL}CFD :${CFD_PORT} listening but pid-file stale (holder=${cfd_holder})${NC}"
  else
    echo -e "${YEL}CFD :${CFD_PORT} not bound${NC}"
  fi
  if [[ "$sb_listen" == "yes" && "$sb_alive" == "yes" ]]; then
    echo -e "${GRN}SB  :${SB_PORT} healthy (pid=${sb_pid_file})${NC}"
  elif [[ "$sb_listen" == "yes" ]]; then
    echo -e "${YEL}SB  :${SB_PORT} listening but pid-file stale (holder=${sb_holder})${NC}"
  else
    echo -e "${YEL}SB  :${SB_PORT} not bound${NC}"
  fi
  dual_supervision_status_line
}

cmd="${1:-start}"
case "$cmd" in
  start) start_both ;;
  status) print_status_block ;;
  stop) stop_both ;;
  dry-run)
    require_python
    ensure_dirs
    pause_legacy_watchdog
    _core_detached_upper="$(printf '%s' "${CORE_DETACHED:-FALSE}" | tr '[:lower:]' '[:upper:]')"
    if [[ "$_core_detached_upper" == "TRUE" ]]; then
      echo -e "${YEL}dry-run OK (CORE_DETACHED=TRUE — order dispatch suppressed on start)${NC}"
    else
      echo "dry-run OK — plist at ${V32_DUAL_PLIST_DST} (no engines started)"
    fi
    echo "Maintenance dry-run: CORE_DETACHED=TRUE ./scripts/v32_runtime_start.sh dry-run"
    ;;
  *)
    echo "Usage: $0 [start|status|stop|dry-run]" >&2
    exit 2
    ;;
esac
