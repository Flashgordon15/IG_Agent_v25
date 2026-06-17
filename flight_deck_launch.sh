#!/usr/bin/env bash
# IG Agent v29.1 — Day 1 Genesis Flight Deck production launch
# Pre-flight → 32 tests → port cleanup → genesis purge → agent + cockpit
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PATH="${ROOT}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export VIRTUAL_ENV="${ROOT}/.venv"
export IG_AGENT_ROOT="${ROOT}"
export DAY1_GENESIS=1
export PYTHONDONTWRITEBYTECODE=1
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export IG_AGENT_FROM_LAUNCHER=1

# Headless credential bridge — secrets live in gitignored config/credentials/launch.env
LAUNCH_ENV="${ROOT}/config/credentials/launch.env"
if [[ -f "${LAUNCH_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${LAUNCH_ENV}"
  set +a
fi
if [[ -n "${AGENT_LAUNCH_PASS:-}" ]]; then
  export AGENT_LAUNCH_PASS
  export ADMIN_PASSWORD="${ADMIN_PASSWORD:-${AGENT_LAUNCH_PASS}}"
  echo "[env] AGENT_LAUNCH_PASS injected for headless IG REST auth"
fi

PY="${ROOT}/.venv/bin/python3"
DESKTOP_LAUNCH="${IG_AGENT_DESKTOP_LAUNCH:-0}"

alert_fail() {
  local msg="$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"IG Agent Flight Deck\" message \"${msg}\" as warning" 2>/dev/null || true
  fi
  echo "ERROR: ${msg}"
}
if [[ ! -x "$PY" ]]; then
  echo "ERROR: missing venv at ${ROOT}/.venv"
  exit 1
fi

echo "=== IG Agent Day 1 Genesis Flight Deck Launch ==="
echo "Root: ${ROOT}"
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

echo ""
echo "[1/5] Pre-flight checks..."
if ! "$PY" scripts/pre_flight_check.py; then
  if [[ "${DESKTOP_LAUNCH}" == "1" ]]; then
    echo "WARN: Pre-flight failed — desktop launch continuing (run flight_deck_launch.sh in Terminal for full gate)"
  else
    alert_fail "Pre-flight check failed — see src/data/logs/desktop_flight_deck.log"
    exit 1
  fi
fi

echo ""
if [[ "${DESKTOP_LAUNCH}" == "1" ]]; then
  echo "[2/5] Desktop shortcut launch — skipping pytest gate (full gate: run flight_deck_launch.sh in Terminal)"
else
  echo "[2/5] Intelligence + cockpit unit gate..."
  "$PY" -m pytest \
    tests/test_target_engine.py \
    tests/test_cockpit_avionics.py \
    tests/test_intelligence_*.py \
    tests/test_liquidity_wave.py \
    tests/test_day1_genesis_reset.py \
    tests/test_premium_overnight.py \
    -q --tb=line
fi

echo ""
echo "[2b/5] Opening Flight Deck web cockpit will be at http://127.0.0.1:8787/"

echo ""
echo "[3/5] Port cleanup (8080 agent + 8787 cockpit)..."
"$PY" - <<'PY'
from cockpit.port_cleanup import clear_port_8080
killed = clear_port_8080()
print(f"Port 8080 cleared PIDs: {killed}" if killed else "Port 8080 clear")
import socket
for port in (8787,):
    try:
        import subprocess, os, signal
        r = subprocess.run(
            ["lsof", "-iTCP", f":{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
        for pid in r.stdout.strip().splitlines():
            pid = pid.strip()
            if pid.isdigit() and int(pid) not in (os.getpid(), os.getppid()):
                os.kill(int(pid), signal.SIGTERM)
                print(f"Port {port} SIGTERM PID {pid}")
    except Exception as e:
        print(f"Port {port} cleanup note: {e}")
PY

echo ""
echo "[3b/5] Night-matrix lockdown: legacy 20:00-06:00 blackout DELETED; rollover lock 21:58-22:05 only"

echo ""
echo "[4/5] Day 1 Genesis Data Reset Protocol..."
"$PY" scripts/day1_genesis_reset.py

echo ""
echo "[5/5] Launching core agent + web Flight Deck (DAY1_GENESIS=1)..."
export IG_AGENT_OPEN_COCKPIT=1
exec "$PY" src/main.py
