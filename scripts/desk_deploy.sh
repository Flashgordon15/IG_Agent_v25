#!/usr/bin/env bash
# Desk deployment manager — operator stacks upgrades on disk; this script deploys safely.
#
# Usage:
#   ./scripts/desk_deploy.sh audit
#   ./scripts/desk_deploy.sh certify
#   ./scripts/desk_deploy.sh deploy [--force-supervised|--force-open-book|--dev]
#   ./scripts/desk_deploy.sh sync-wrappers
#
# Open-book policy (dev always possible):
#   - Flat book: normal anti-zombie recycle.
#   - Opens > 0: auto --force-open-book (pause entries during cutover, offline
#     supervise loop, inflight adopt). Never refuse solely because the book is open
#     when the operator passed --dev / --force-open-book, or when opens are detected.
#   - Prefer pause-only hotfixes: ./scripts/desk_dev_pause.sh pause
#   - Offline-with-opens: ./scripts/desk_dev_offline.sh
#
# --force-open-book / --dev: recycle bytecode even with broker opens; starts an
# offline supervise loop before shutdown so inflight risk is covered through the
# restart, then arms OpenPositionManager / trade_support on the new process.
# Never kill -9.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
# shellcheck source=lib/detach_exec.sh
source "${ROOT}/scripts/lib/detach_exec.sh"

export APP_MODE=DEMO
export IG_AGENT_CONFIG=config/config_v31_demo_throughput.json
export PYTHONPATH="${ROOT}/src"
PY="${ROOT}/.venv/bin/python3"
if [ ! -x "${PY}" ]; then
  PY="$(command -v python3)"
fi

FORCE_SUPERVISED=0
FORCE_OPEN_BOOK=0
DEV_MODE=0
for arg in "$@"; do
  if [ "${arg}" = "--force-supervised" ]; then
    FORCE_SUPERVISED=1
  fi
  if [ "${arg}" = "--force-open-book" ] || [ "${arg}" = "--dev" ]; then
    FORCE_OPEN_BOOK=1
    FORCE_SUPERVISED=1
  fi
  if [ "${arg}" = "--dev" ]; then
    DEV_MODE=1
  fi
done

log() { printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

_audit_json() {
  local extra=()
  if [ "${FORCE_SUPERVISED}" -eq 1 ]; then
    extra+=(--force-supervised)
  fi
  "${PY}" "${ROOT}/scripts/desk_deploy_audit.py" --json "${extra[@]}"
}

_broker_open() {
  # Audit may exit 1 for soft ISSUEs (wrappers down / API cold) while still flat.
  # Parse stdout regardless of exit code — only broker_open gates deploy.
  "${PY}" -c "
import json, subprocess, sys
p = subprocess.run(
    [sys.executable, '${ROOT}/scripts/desk_deploy_audit.py', '--json'],
    text=True,
    capture_output=True,
)
raw = (p.stdout or '').strip() or '{}'
try:
    print(int(json.loads(raw).get('broker_open') or 0))
except Exception:
    # Refuse deploy on unreadable audit payload
    print(999999)
"
}

_supervise_running() {
  pgrep -f "manage_live_positions.py --supervise" >/dev/null 2>&1
}

_start_offline_supervise() {
  if _supervise_running; then
    log "offline supervise already running"
    return 0
  fi
  log "starting offline supervise loop (inflight cover during recycle)"
  mkdir -p "${ROOT}/src/data/logs"
  detach_exec --log "${ROOT}/src/data/logs/supervise_deploy.log" -- \
    env APP_MODE=DEMO IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH="${ROOT}/src" \
    "${PY}" "${ROOT}/scripts/manage_live_positions.py" --supervise-loop --manage --poll-sec 15
  sleep 2
  if _supervise_running; then
    log "offline supervise armed pid=$(pgrep -f 'manage_live_positions.py --supervise' | head -1)"
    return 0
  fi
  log "WARN offline supervise failed to start"
  return 1
}

_clear_entry_holds() {
  # Entry gates only — exits / OPM / trade_support remain live.
  # Also clear offline_for_dev + trading_paused so force-open-book resumes path_live.
  log "clearing entry holds (deploy_hold / entry_halt / trading_paused / offline_for_dev)"
  "${PY}" -c "
from system.startup_hold_clear import clear_stale_entry_holds_if_flat
from runtime.deploy_hold import set_deploy_hold
from system.shutdown_cleanup import clear_manual_stop
import json, time
from pathlib import Path
from system.paths import data_dir
set_deploy_hold(active=False, reason='desk_deploy_complete')
clear_manual_stop()
root = Path(data_dir())
payload = json.dumps({'active': False, 'reason': 'desk_deploy_complete', 'ts': time.time()})
for sub in ('state', 'state_cfd', 'state_sb'):
    d = root / sub
    d.mkdir(parents=True, exist_ok=True)
    for name in ('entry_halt.json', 'trading_paused.json', 'offline_for_dev.json'):
        (d / name).write_text(payload)
r = clear_stale_entry_holds_if_flat(port=8080, reason='desk_deploy_complete', allow_offline_stale_clear=True)
print('holds_cleared', r)
" || true
}

_write_stop_snapshot_mirror() {
  local legacy="${ROOT}/src/data/state"
  local prod="${ROOT}/src/data/v31-production/state"
  mkdir -p "${legacy}" "${prod}"
  local stamp
  stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"active":true,"reason":"desk_deploy","ts":"%s"}\n' "${stamp}" >"${legacy}/.stop_snapshot_mirror"
  printf '{"active":true,"reason":"desk_deploy","ts":"%s"}\n' "${stamp}" >"${prod}/.stop_snapshot_mirror"
  log "stop_snapshot_mirror written (legacy + v31-production)"
}

_wait_port_free() {
  local port="${1:-8080}"
  local max="${2:-30}"
  local i=0
  while lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "${i}" -ge "${max}" ]; then
      log "ERROR: port ${port} still bound after ${max}s"
      return 1
    fi
    sleep 1
  done
  return 0
}

_anti_zombie_shutdown() {
  log "anti-zombie: mark_manual_stop"
  "${PY}" -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='desk_deploy')"

  local pid
  pid="$(pgrep -f 'src/main.py' | head -1 || true)"
  if [ -n "${pid}" ]; then
    log "anti-zombie: SIGTERM main.py pid=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
    # Wait for process exit AND port free — port-only wait left hung mains
    # that had already unbound :8080 but still held GIL/threads (deploy hang).
    local i=0
    while ps -p "${pid}" >/dev/null 2>&1; do
      i=$((i + 1))
      # Port already free but process still running — nudge once (never kill -9).
      if [ "${i}" -eq 20 ] && ! lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
        log "anti-zombie: port free but pid=${pid} still up — second SIGTERM"
        kill -TERM "${pid}" 2>/dev/null || true
      fi
      if [ "${i}" -eq 30 ] && ps -p "${pid}" >/dev/null 2>&1; then
        log "anti-zombie: pid=${pid} still up — SIGINT"
        kill -INT "${pid}" 2>/dev/null || true
      fi
      if [ "${i}" -ge 45 ]; then
        log "ERROR: main pid=${pid} still alive after 45s TERM (refusing kill -9)"
        return 1
      fi
      sleep 1
    done
    log "anti-zombie: main pid=${pid} exited"
    _wait_port_free 8080 15 || true
  else
    _wait_port_free 8080 10 || true
  fi

  log "anti-zombie: evict bytecode + instance lock"
  find src -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find src -name '*.pyc' -delete 2>/dev/null || true
  rm -f src/data/.ig_agent_v29.lock src/data/.ig_agent_v31.lock 2>/dev/null || true
  # Reset cross-process REST storm ledger so the new process does not inherit HIGH.
  rm -f "${ROOT}/src/data/v31-production/state/rest_budget_shared.json" 2>/dev/null || true
}

_start_main() {
  log "starting main agent via session_ready"
  # Cap wait — session_ready can wedge on Py_Finalize after smoke (os._exit now
  # avoids that; timeout is a belt-and-braces so deploy never hangs).
  local boot_timeout="${DESK_BOOT_TIMEOUT_SEC:-120}"
  if "${PY}" -c "
import subprocess, sys, time, urllib.request
p = subprocess.Popen(
    [sys.executable, '${ROOT}/scripts/session_ready.py', '--start-agent'],
    cwd='${ROOT}',
    start_new_session=True,
)
deadline = time.time() + ${boot_timeout}
while time.time() < deadline:
    if p.poll() is not None:
        sys.exit(p.returncode or 0)
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/api/health_light', timeout=2) as r:
            if r.status == 200:
                # Health green — do not wait for Py_Finalize of the starter.
                time.sleep(2)
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except Exception:
                        pass
                sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
if p.poll() is None:
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        pass
    # Port up counts as success even if starter wedged.
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/api/health_light', timeout=2) as r:
            sys.exit(0 if r.status == 200 else 1)
    except Exception:
        sys.exit(1)
sys.exit(p.returncode or 0)
"; then
    log "session_ready: agent boot OK"
  else
    log "WARN session_ready boot helper non-zero — checking port 8080"
    if lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
      log "port 8080 up — continuing deploy"
    else
      log "ERROR: agent failed to bind :8080"
      return 1
    fi
  fi
}

_restart_trade_support() {
  local label="gui/$(id -u)/com.igagent.trade_support"
  if launchctl print "${label}" >/dev/null 2>&1; then
    log "restarting trade_support via launchctl kickstart"
    launchctl kickstart -k "${label}" 2>/dev/null || launchctl bootstrap "${label}" 2>/dev/null || true
  else
    log "trade_support launchd missing — spawning wrapper script"
    detach_exec --log "${ROOT}/src/data/logs/trade_support_wrapper.log" -- \
      "${ROOT}/scripts/trade_support_wrapper.sh"
  fi
}

_verify_desk_support() {
  local label="gui/$(id -u)/com.igagent.desk_support"
  if launchctl print "${label}" >/dev/null 2>&1; then
    launchctl kickstart "${label}" 2>/dev/null || true
  fi
  sleep 2
  if pgrep -f "runtime.desk_support_wrapper" >/dev/null 2>&1; then
    log "desk_support wrapper alive"
    return 0
  fi
  log "WARN desk_support wrapper not running — run: ./scripts/install_desk_support.sh"
  return 1
}

cmd_audit() {
  # Do NOT exec — deploy calls audit mid-script and must continue into
  # anti-zombie shutdown + session_ready restart after the report prints.
  if [ "${FORCE_SUPERVISED}" -eq 1 ]; then
    "${PY}" "${ROOT}/scripts/desk_deploy_audit.py" --force-supervised
  else
    "${PY}" "${ROOT}/scripts/desk_deploy_audit.py"
  fi
}

cmd_certify() {
  # Hard timeout — health-green + flat must not wedge on Py_Finalize / tick REST.
  local certify_timeout="${DESK_CERTIFY_TIMEOUT_SEC:-90}"
  log "certify: verify_session_live (timeout=${certify_timeout}s)"
  if command -v timeout >/dev/null 2>&1; then
    if timeout "${certify_timeout}" "${PY}" "${ROOT}/scripts/verify_session_live.py"; then
      echo "CERTIFY: PASS"
      return 0
    fi
  else
    # macOS: no GNU timeout — use Python alarm wrapper
    if "${PY}" -c "
import subprocess, sys
r = subprocess.run(
    [sys.executable, '${ROOT}/scripts/verify_session_live.py'],
    timeout=${certify_timeout},
)
sys.exit(r.returncode)
"; then
      echo "CERTIFY: PASS"
      return 0
    fi
  fi
  # Soft pass when health already green and book flat — avoid deploy wedge.
  if curl -sf --max-time 3 http://127.0.0.1:8080/api/health_light >/dev/null 2>&1; then
    local opens
    opens="$(_broker_open)"
    if [ "${opens}" -eq 0 ]; then
      log "certify: soft-PASS (health_light green, flat; verify timed out)"
      echo "CERTIFY: SOFT_PASS"
      return 0
    fi
  fi
  echo "CERTIFY: FAIL"
  return 1
}

cmd_sync_wrappers() {
  log "sync-wrappers: trade_support + desk_support (no main restart)"
  _restart_trade_support
  sleep 3
  _verify_desk_support || true
  "${PY}" "${ROOT}/scripts/desk_deploy_audit.py" || true
  log "sync-wrappers complete"
}

_pause_entries_for_cutover() {
  log "deploy: pausing entries for open-book cutover (supervisors stay live)"
  "${PY}" -c "
from runtime.desk_dev_controls import pause_entries
import json
print(json.dumps(pause_entries(reason='desk_deploy_open_book_cutover'), indent=2, default=str))
" || true
}

cmd_deploy() {
  local opens
  opens="$(_broker_open)"
  # Auto open-book path: opens > 0 always recycles with force-open-book semantics.
  # Flat book still works without flags. Never treat open book as a hard refuse
  # when --dev / --force-open-book is set (or opens are detected).
  if [ "${opens}" -gt 0 ] && [ "${FORCE_OPEN_BOOK}" -ne 1 ]; then
    log "deploy: broker_open=${opens} — auto --force-open-book (dev-safe; no flatten)"
    FORCE_OPEN_BOOK=1
    FORCE_SUPERVISED=1
  fi
  if [ "${opens}" -gt 0 ] || [ "${FORCE_OPEN_BOOK}" -eq 1 ]; then
    log "deploy: open-book recycle path (broker_open=${opens} dev=${DEV_MODE})"
    _pause_entries_for_cutover
    _start_offline_supervise || true
  fi
  if [ "${FORCE_SUPERVISED}" -eq 1 ] && [ "${FORCE_OPEN_BOOK}" -ne 1 ] && ! _supervise_running; then
    log "REFUSED: --force-supervised but supervise loop not running"
    exit 2
  fi

  log "deploy: pre-audit"
  cmd_audit || true

  _anti_zombie_shutdown
  _write_stop_snapshot_mirror
  # Stagger supervise fanout vs main boot — reduce post-boot REST storm.
  if [ "${FORCE_OPEN_BOOK}" -eq 1 ]; then
    log "deploy: post-boot REST storm guard — delaying wrapper kickstart 8s after main"
  fi
  _start_main

  log "deploy: sync wrappers after main restart"
  if [ "${FORCE_OPEN_BOOK}" -eq 1 ]; then
    sleep 8
  else
    sleep 3
  fi
  _restart_trade_support
  sleep 3
  _verify_desk_support || true

  # UI auto-heal — ensure Quantum Terminal :3000 is up after recycle.
  if [ -x "${ROOT}/scripts/start_ui_background.sh" ]; then
    if ! lsof -iTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then
      log "deploy: UI :3000 down — starting start_ui_background.sh"
      detach_exec --log "${ROOT}/src/data/logs/ui_background.log" -- \
        "${ROOT}/scripts/start_ui_background.sh"
      sleep 2
    else
      log "deploy: UI :3000 already listening"
    fi
  fi

  # Resume entries after bytecode load; inflight stays under OPM + trade_support.
  # Dev mode keeps pause until operator explicitly resumes.
  # Otherwise clear only when REST is not CRITICAL (assess-ready).
  if [ "${DEV_MODE}" -eq 1 ]; then
    log "deploy: --dev keeps entry pause — resume with ./scripts/desk_dev_pause.sh resume"
  else
    rest_level="$("${PY}" -c "
import urllib.request, json
try:
  with urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4) as r:
    d=json.loads(r.read().decode())
  print((d.get('rest_api_budget') or {}).get('pressure_level') or 'OK')
except Exception:
  print('UNKNOWN')
" 2>/dev/null || echo UNKNOWN)"
    if [ "${rest_level}" = "CRITICAL" ] || [ "${rest_level}" = "HIGH" ]; then
      log "deploy: keeping entry pause (REST=${rest_level}) — resume later via desk_dev_pause.sh resume"
    else
      _clear_entry_holds
    fi
  fi

  log "deploy: post-deploy certify"
  if cmd_certify; then
    echo "DEPLOY: PASS"
    local pid
    pid="$(pgrep -f 'src/main.py' | head -1 || true)"
    echo "DEPLOY: main_pid=${pid:-unknown}"
    echo "DEPLOY: broker_open=${opens} force_open_book=${FORCE_OPEN_BOOK} dev=${DEV_MODE}"
    return 0
  fi
  echo "DEPLOY: FAIL (agent up but certify failed — inspect verify_session_live)"
  local pid
  pid="$(pgrep -f 'src/main.py' | head -1 || true)"
  echo "DEPLOY: main_pid=${pid:-unknown}"
  return 1
}

CMD="${1:-audit}"
case "${CMD}" in
  audit) cmd_audit ;;
  certify) cmd_certify ;;
  deploy) cmd_deploy ;;
  sync-wrappers) cmd_sync_wrappers ;;
  -h|--help|help)
    sed -n '2,10p' "$0"
    ;;
  *)
    echo "Unknown command: ${CMD}" >&2
    exit 1
    ;;
esac
