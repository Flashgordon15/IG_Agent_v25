#!/usr/bin/env bash
# Complete zero-state deconstruction — launchd, agent, UI, ports, locks, pycache.
# Follows anti-zombie protocol: manual_stop hold → SIGTERM → verify ports → purge.
#
# Usage:
#   ./scripts/absolute_teardown.sh          # abort if markets open or positions exist
#   FORCE=1 ./scripts/absolute_teardown.sh # operator override
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export IG_AGENT_ROOT="$ROOT"
export PYTHONPATH="${ROOT}/src"

PYTHON="${ROOT}/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

PORT_WAIT_SEC="${PORT_WAIT_SEC:-30}"
FORCE="${FORCE:-0}"

echo "=== PRE-DEVELOPMENT AUDIT ==="
_markets_open="$("$PYTHON" - <<'PY' 2>/dev/null || echo unknown
from system.market_watch.calendar import is_market_open

night = [
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
]
print("yes" if any(is_market_open(e) for e in night) else "no")
PY
)"
_positions="unknown"

_rt="${ROOT}/src/data/runtime_state.json"
if [[ -f "$_rt" ]]; then
  _positions="$("$PYTHON" - <<PY 2>/dev/null || echo unknown
import json
from pathlib import Path
d = json.loads(Path("${_rt}").read_text())
entry = (d.get("entry") or {}).get("entries") or []
pending = (d.get("pending") or {}).get("orders") or []
print("yes" if entry or pending else "no")
PY
)"
fi

_hold="$("$PYTHON" - <<'PY' 2>/dev/null || echo unknown
from system.shutdown_cleanup import manual_stop_active
print("yes" if manual_stop_active() else "no")
PY
)"

echo "[Markets open?] ${_markets_open} | [Watchdog hold?] ${_hold} | [Runtime positions?] ${_positions}"
lsof -iTCP:8080 -sTCP:LISTEN 2>/dev/null || echo "Port 8080: free"
lsof -iTCP:3000 -sTCP:LISTEN 2>/dev/null || echo "Port 3000: free"
pgrep -fl "${ROOT}/src/main.py|desktop_cockpit.py|pytest" 2>/dev/null || echo "Agent PIDs: none"
echo "================================"

if [[ "$FORCE" != "1" ]]; then
  if [[ "$_positions" == "yes" ]]; then
    echo "ABORT: runtime_state has open entries or pending orders. Set FORCE=1 to override."
    exit 1
  fi
  if [[ "$_markets_open" == "yes" ]]; then
    echo "ABORT: night-matrix markets are open. Set FORCE=1 to override."
    exit 1
  fi
fi

echo "=== INITIALISING COMPLETE ZERO-STATE DECONSTRUCTION ==="

# 0. Engage watchdog hold so launchd does not respawn during teardown
echo "[0/6] Engaging manual_stop hold (blocks launchd auto-restart)..."
"$PYTHON" - <<'PY'
from system.shutdown_cleanup import mark_manual_stop
mark_manual_stop(source="absolute_teardown")
print("manual_stop: engaged")
PY

# 1. Surgically unload macOS LaunchAgent daemons
echo "[1/6] Unloading and removing launchd automated jobs..."
launchctl bootout "gui/$(id -u)/com.igagent.v30.ui" 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.igagent* 2>/dev/null || true
rm -rf ~/Library/LaunchAgents/com.igagent*
rm -rf /Library/LaunchAgents/com.igagent*

# 2. Terminate Next.js UI (port 3000 / project terminal only — not all node)
echo "[2/6] Stopping Next.js UI on :3000..."
if command -v lsof >/dev/null 2>&1; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    kill -TERM "$pid" 2>/dev/null || true
  done < <(lsof -t -iTCP:3000 -sTCP:LISTEN 2>/dev/null || true)
fi
sleep 1
while read -r pid; do
  [[ -z "$pid" ]] && continue
  kill -KILL "$pid" 2>/dev/null || true
done < <(lsof -t -iTCP:3000 -sTCP:LISTEN 2>/dev/null || true)
while read -r pid; do
  [[ -z "$pid" ]] && continue
  kill -TERM "$pid" 2>/dev/null || true
done < <(pgrep -f "${ROOT}/terminal/.*next" 2>/dev/null || true)

# 3. Graceful agent + test shutdown (SIGTERM first)
echo "[3/6] Terminating agent, cockpit, and pytest (SIGTERM)..."
SELF=$$
_kill_pattern() {
  local sig=$1
  local pattern=$2
  while read -r pid; do
    [[ -z "$pid" || "$pid" == "$SELF" ]] && continue
    kill "$sig" "$pid" 2>/dev/null || true
  done < <(pgrep -f "$pattern" 2>/dev/null || true)
}
for pattern in \
  "${ROOT}/src/main.py" \
  "${ROOT}/scripts/desktop_cockpit.py" \
  "pytest.*${ROOT}/tests" \
  "${ROOT}/.venv/bin/python.*pytest"
do
  _kill_pattern TERM "$pattern"
done

for _ in $(seq 1 "$PORT_WAIT_SEC"); do
  if ! lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Escalate only if still alive
echo "[4/6] Escalating stubborn PIDs and clearing ports 8080 / 49151..."
for pattern in \
  "${ROOT}/src/main.py" \
  "${ROOT}/scripts/desktop_cockpit.py" \
  "pytest.*${ROOT}/tests"
do
  _kill_pattern KILL "$pattern"
done
killall -9 Python 2>/dev/null || true
while read -r pid; do
  [[ -z "$pid" ]] && continue
  kill -KILL "$pid" 2>/dev/null || true
done < <(
  {
    lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null || true
    lsof -t -iTCP:49151 -sTCP:LISTEN 2>/dev/null || true
  } | sort -u
)

if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "ERROR: port 8080 still bound after ${PORT_WAIT_SEC}s — teardown incomplete." >&2
  exit 1
fi

# 5. Wipe locks, temp markers, and in-flight state
echo "[5/6] Purging locks, PID markers, and ephemeral state..."
rm -f \
  src/data/state/alpha_matrix_publisher.pid \
  src/data/state/ui_terminal_3000.pid \
  src/data/.ig_agent_v29.lock \
  src/data/.ig_agent_v30_port_8080.lock \
  src/data/watchdog.pid
rm -f src/data/state/fulfillment_cache.json src/data/state/order_inflight.json
rm -rf /var/tmp/ig_agent* /tmp/ig_agent* ~/.ig_agent* ~/.ig_agent_runtime.lock

# 6. Pycache purge (only after port 8080 is free)
echo "[6/6] Purging compiled pycache under src/..."
find src -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find src -name '*.pyc' -delete 2>/dev/null || true

echo "=== SYSTEM OFFLINE. PORTS CLEAR. LOCKS PURGED. COMPILER CLEAN. ==="
