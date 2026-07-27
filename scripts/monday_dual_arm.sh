#!/usr/bin/env bash
# ============================================================================
# monday_dual_arm.sh — STAGED live dual-arm for Monday cash reopen.
#
# Arms BOTH desks for REAL orders as one operator-gated action:
#   :8080  Z6BAH4  QUANT_SNIPER   → CFD sniper (micro)   — A2 hard-block lifted here
#   :8081  Z6BAH3  MACRO_SENTINEL → SB macro / long_trade_runner (LTR)
#
# SAFETY CONTRACT (why this script exists instead of a raw POST /api/start):
#   * Instant/micro stays HARD OFF on SB — SB runs macro/LTR only (Path A carve).
#   * Refuses to arm on Sat/Sun so it CANNOT run over the weekend by accident.
#   * Refuses mid-rollover 21:58–22:05 BST (sole scheduled institutional block).
#   * Refuses unless BOTH books are FLAT.
#   * Lifts state_cfd/a2_entries_paused.json hard_block ONLY inside `arm`.
#   * Stamps a fresh operator_reopen_witness.json before arming.
#   * Clears bleed locks / entry holds ONLY when present, and only on `arm`.
#   * Preserves supervision (trade_support, desk_support, v32 dual watchdog).
#   * Never kill -9. Never flattens. Session-kill / bleed alarms stay armed.
#
# COMMANDS:
#   ./scripts/monday_dual_arm.sh preflight   # read-only readiness (default)
#   ./scripts/monday_dual_arm.sh dry-run     # show exactly what arm WOULD do
#   ./scripts/monday_dual_arm.sh arm         # EXECUTE (Monday cash open only)
#
# This script does NOT schedule itself. Run it by hand at Monday open (or wire
# a launchd job only AFTER a clean dry-run — see docs/DESK_REOPEN_CHECKLIST.md).
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"

PY="${ROOT}/.venv/bin/python3"
[ -x "${PY}" ] || PY="$(command -v python3)"

CFD_PORT="${IG_API_PORT_CFD:-8080}"
SB_PORT="${IG_API_PORT_SB:-8081}"
DATA_ROOT="${ROOT}/src/data/v31-production"
A2_MARKER="${DATA_ROOT}/state_cfd/a2_entries_paused.json"

RED='\033[0;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'
say()  { echo -e "$*"; }
ok()   { echo -e "${GRN}OK${NC}   $*"; }
warn() { echo -e "${YEL}WARN${NC} $*" >&2; }
die()  { echo -e "${RED}ABORT${NC} $*" >&2; exit 1; }

# --- guards ---------------------------------------------------------------
guard_not_weekend() {
  local force="${1:-0}"
  local dow; dow="$(date +%u)"   # 1=Mon .. 7=Sun
  if [[ "${dow}" == "6" || "${dow}" == "7" ]]; then
    if [[ "${force}" == "1" ]]; then
      warn "Weekend detected (dow=${dow}) but --force-weekend set — proceeding."
      return 0
    fi
    die "Weekend (dow=${dow}). Markets shut — dual-arm refuses. Run Monday cash open (or --force-weekend to override once markets are genuinely open)."
  fi
}

guard_not_rollover() {
  local hm; hm="$(TZ=Europe/London date +%H%M)"
  # 21:58–22:05 BST institutional day-clear.
  if [[ "${hm}" -ge 2158 && "${hm}" -le 2205 ]]; then
    die "Mid-rollover ${hm} BST (21:58–22:05). Dual-arm refuses during the institutional day-clear."
  fi
  ok "Rollover window clear (now ${hm} BST)."
}

# Returns 0 when the port reports FLAT (verdict FLAT + count 0), else 1.
port_flat() {
  local port="$1"
  local verdict
  verdict="$(curl -sf --max-time 5 "http://127.0.0.1:${port}/api/positions/live" 2>/dev/null \
    | "${PY}" -c "import sys,json;
try:
    d=json.load(sys.stdin)
    print('FLAT' if str(d.get('verdict'))=='FLAT' and int(d.get('count',0))==0 else 'OPEN')
except Exception:
    print('UNKNOWN')" 2>/dev/null || echo "UNREACHABLE")"
  echo "${verdict}"
}

port_alive() {
  local port="$1"
  curl -sf --max-time 5 "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1 && echo yes || echo no
}

a2_active() {
  "${PY}" -c "
import json
from pathlib import Path
p=Path('${A2_MARKER}')
if not p.is_file():
    print('inactive'); raise SystemExit
try:
    r=json.loads(p.read_text())
except Exception:
    print('active'); raise SystemExit
print('active' if r.get('active') is True else 'inactive')"
}

post_start() {
  local port="$1"
  if curl -sf --max-time 8 -X POST "http://127.0.0.1:${port}/api/start" >/dev/null 2>&1; then
    ok ":${port} POST /api/start accepted"
  else
    warn ":${port} POST /api/start failed/unreachable"
    return 1
  fi
}

print_watch_criteria() {
  cat <<EOF

------------------------------------------------------------------------
WATCH CRITERIA (both ports) — first 30 min after arm:
  curl -s http://127.0.0.1:${CFD_PORT}/api/health            # agent_alive, trade_ready
  curl -s http://127.0.0.1:${CFD_PORT}/api/trade_support/status
  curl -s http://127.0.0.1:${CFD_PORT}/api/positions/live    # verdict/critical/broker_open_sot
  curl -s http://127.0.0.1:${SB_PORT}/api/positions/live
  curl -s http://127.0.0.1:${CFD_PORT}/api/rotation_state

KILL SWITCHES (either port — never kill -9 main):
  ./scripts/desk_dev_pause.sh pause          # freeze NEW entries, keep supervision
  curl -s -X POST http://127.0.0.1:${CFD_PORT}/api/stop     # pause CFD
  curl -s -X POST http://127.0.0.1:${SB_PORT}/api/stop      # pause SB
  # Re-instate A2 CFD hard-block:
  ${PY} -c "import json,time;from pathlib import Path;p=Path('${A2_MARKER}');\
d=json.loads(p.read_text());d['active']=True;p.write_text(json.dumps(d,indent=2))"
  # Full anti-zombie teardown: ./scripts/desk_deploy.sh audit  (see runbook)
------------------------------------------------------------------------
EOF
}

# --- readiness (shared by all modes) --------------------------------------
readiness_report() {
  say "=== MONDAY DUAL-ARM READINESS ==="
  say "Data root: ${DATA_ROOT}"
  local cfd_alive sb_alive cfd_flat sb_flat a2 cfd_opm sb_opm
  cfd_alive="$(port_alive "${CFD_PORT}")"
  sb_alive="$(port_alive "${SB_PORT}")"
  cfd_flat="$(port_flat "${CFD_PORT}")"
  sb_flat="$(port_flat "${SB_PORT}")"
  a2="$(a2_active)"
  cfd_opm="$(curl -sf --max-time 5 "http://127.0.0.1:${CFD_PORT}/api/position_manager/status" 2>/dev/null \
    | "${PY}" -c "import sys,json
try:
 d=json.load(sys.stdin); print('active' if d.get('active') else 'inactive')
except Exception:
 print('unreachable')" 2>/dev/null || echo "unreachable")"
  sb_opm="$(curl -sf --max-time 5 "http://127.0.0.1:${SB_PORT}/api/position_manager/status" 2>/dev/null \
    | "${PY}" -c "import sys,json
try:
 d=json.load(sys.stdin); print('active' if d.get('active') else 'inactive')
except Exception:
 print('unreachable')" 2>/dev/null || echo "unreachable")"
  say "CFD :${CFD_PORT} alive=${cfd_alive} book=${cfd_flat} OPM=${cfd_opm}"
  say "SB  :${SB_PORT} alive=${sb_alive} book=${sb_flat} OPM=${sb_opm}"
  say "A2 CFD hard-block: ${a2}"
  say "Now: $(TZ=Europe/London date '+%Y-%m-%d %H:%M:%S %Z') (dow=$(date +%u))"
  # export for caller checks
  READY_CFD_FLAT="${cfd_flat}"; READY_SB_FLAT="${sb_flat}"
  READY_A2="${a2}"
  READY_CFD_OPM="${cfd_opm}"; READY_SB_OPM="${sb_opm}"
  if [[ "${cfd_opm}" != "active" || "${sb_opm}" != "active" ]]; then
    warn "OPM inactive (CFD=${cfd_opm} SB=${sb_opm}) — arm will refuse until both active."
  else
    ok "OPM.active=true on both twins"
  fi
}

require_flat_both() {
  [[ "${READY_CFD_FLAT}" == "FLAT" ]] || die "CFD :${CFD_PORT} not FLAT (${READY_CFD_FLAT}). Refusing arm."
  [[ "${READY_SB_FLAT}" == "FLAT" ]]  || die "SB :${SB_PORT} not FLAT (${READY_SB_FLAT}). Refusing arm."
  ok "Both books FLAT."
}

require_opm_active_both() {
  [[ "${READY_CFD_OPM}" == "active" ]] || die "CFD OPM not active (${READY_CFD_OPM}). Refusing arm — fix OpenPositionManager first."
  [[ "${READY_SB_OPM}" == "active" ]]  || die "SB OPM not active (${READY_SB_OPM}). Refusing arm — fix OpenPositionManager first."
  ok "OPM.active on both twins."
}

# --- arm actions ----------------------------------------------------------
stamp_witness() {
  "${PY}" -c "
from pathlib import Path
from runtime.gui_desk_supervisor import write_reopen_witness
b = write_reopen_witness(Path('${DATA_ROOT}'), reason='monday_dual_arm_live_reopen')
print('witness stamped:', b.get('reopened_at'))"
}

clear_holds_and_bleed_locks() {
  # Clear desk_dev entry holds (no process start here — start is explicit below).
  "${PY}" -c "
from runtime.desk_dev_controls import resume_entries, status_snapshot
import json
print('resume_entries:', json.dumps(resume_entries(reason='monday_dual_arm'), default=str))" || warn "resume_entries failed (continuing)"
  # Clear operator bleed locks ONLY if present (operator-gated unlock).
  "${PY}" -c "
import json
from pathlib import Path
root=Path('${DATA_ROOT}')
cleared=[]
for sub in ('state_cfd','state_sb','state'):
    d=root/sub
    if not d.is_dir(): continue
    for p in sorted(d.glob('operator_bleed_lock_*.json')):
        try:
            r=json.loads(p.read_text())
        except Exception:
            r={}
        if isinstance(r,dict) and r.get('active') is not False:
            r['active']=False; r['deactivated_by']='monday_dual_arm'
            p.write_text(json.dumps(r,indent=2))
            cleared.append(str(p))
print('bleed_locks_cleared:', cleared or 'none present')"
}

lift_a2_hard_block() {
  "${PY}" -c "
import json, datetime
from pathlib import Path
p=Path('${A2_MARKER}')
if not p.is_file():
    print('A2 marker absent — nothing to lift'); raise SystemExit
r=json.loads(p.read_text())
r['active']=False
r['deactivated_at']=datetime.datetime.now().astimezone().isoformat()
r['deactivated_by']='monday_dual_arm'
r['note']='lifted as part of staged Monday dual-arm (CFD sniper live)'
p.write_text(json.dumps(r,indent=2))
print('A2 hard-block lifted (active=false)')"
}

verify_armed() {
  say ""
  say "=== POST-ARM VERIFICATION ==="
  local cfd_flat sb_flat a2 cfd_pause sb_pause
  cfd_flat="$(port_flat "${CFD_PORT}")"
  sb_flat="$(port_flat "${SB_PORT}")"
  a2="$(a2_active)"
  cfd_pause="$([[ -f "${DATA_ROOT}/state_cfd/trading_paused.json" ]] && echo present || echo absent)"
  sb_pause="$([[ -f "${DATA_ROOT}/state_sb/trading_paused.json" ]] && echo present || echo absent)"
  say "CFD book=${cfd_flat} pause_flag=${cfd_pause}"
  say "SB  book=${sb_flat} pause_flag=${sb_pause}"
  say "A2 CFD hard-block: ${a2}"
  [[ "${a2}" == "inactive" ]] && ok "A2 inactive" || warn "A2 still active"
  [[ "${cfd_pause}" == "absent" ]] && ok "CFD paused=false" || warn "CFD pause flag still present"
  [[ "${sb_pause}" == "absent" ]] && ok "SB paused=false" || warn "SB pause flag still present"
}

rearm_watchdog_note() {
  say ""
  say "Supervision: if either twin was recycled, re-arm the v32 dual watchdog:"
  say "  ./scripts/install_v32_dual_watchdog.sh"
  say "(trade_support + desk_support wrappers remain in-process; this only re-arms launchd.)"
}

# --- main -----------------------------------------------------------------
CMD="${1:-preflight}"
FORCE_WEEKEND=0
for a in "$@"; do [[ "$a" == "--force-weekend" ]] && FORCE_WEEKEND=1; done

case "${CMD}" in
  preflight)
    readiness_report
    say ""
    say "Preflight only — no changes made. Run 'dry-run' to preview arm, 'arm' to execute (Monday)."
    ;;

  dry-run)
    readiness_report
    guard_not_rollover
    say ""
    say "=== DRY-RUN: actions 'arm' WOULD take (NO changes made) ==="
    say " 1. guard: refuse if weekend / rollover / not FLAT both ports"
    say " 2. stamp operator_reopen_witness.json (reason=monday_dual_arm_live_reopen)"
    say " 3. clear desk_dev entry holds + deactivate any active operator_bleed_lock_*.json"
    say " 4. lift A2 CFD hard-block: ${A2_MARKER} -> active=false"
    say " 5. POST /api/start on :${CFD_PORT} (CFD sniper) AND :${SB_PORT} (SB macro/LTR)"
    say " 6. verify: A2 inactive, both pause flags absent, both books FLAT"
    say " 7. SB Instant/micro remains HARD OFF (macro/LTR carve untouched)"
    print_watch_criteria
    ;;

  arm)
    guard_not_weekend "${FORCE_WEEKEND}"
    readiness_report
    guard_not_rollover
    require_flat_both
    require_opm_active_both
    say ""
    say "=== ARMING DUAL DESK (live orders) ==="
    stamp_witness
    clear_holds_and_bleed_locks
    lift_a2_hard_block
    post_start "${CFD_PORT}" || warn "CFD start not confirmed"
    post_start "${SB_PORT}"  || warn "SB start not confirmed"
    sleep 3
    verify_armed
    rearm_watchdog_note
    print_watch_criteria
    say ""
    ok "Dual-arm sequence complete. Supervise closely for the first session."
    ;;

  -h|--help|help)
    sed -n '2,33p' "$0"
    ;;

  *)
    die "Unknown command: ${CMD} (preflight|dry-run|arm)"
    ;;
esac
