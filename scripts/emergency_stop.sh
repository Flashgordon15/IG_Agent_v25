#!/usr/bin/env bash
# Emergency stop — close IG positions, disable live trading, kill agent, write lock.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

export IG_AGENT_ROOT="$ROOT"
export PYTHONPATH="${ROOT}/src"

PYTHON="${PYTHON:-python3}"
if [[ -x "${ROOT}/.venv/bin/python3" ]]; then
  PYTHON="${ROOT}/.venv/bin/python3"
elif [[ -x "${ROOT}/venv/bin/python3" ]]; then
  PYTHON="${ROOT}/venv/bin/python3"
fi

# 1. Close all open IG positions via REST API
"$PYTHON" - <<'PY' || echo "WARN: position close failed — continuing" >&2
import sys
import time

import os
from pathlib import Path

from system.config_loader import ConfigLoader
from system.credentials_loader import try_load_credentials
from system.ig_rest_session import ensure_shared_authenticated

status = try_load_credentials()
if not status.ok or status.credentials is None:
    print("WARN: credentials missing — cannot close positions", file=sys.stderr)
    sys.exit(0)

creds = status.credentials
root = Path(os.environ["IG_AGENT_ROOT"])
cfg = ConfigLoader(root / "config" / "config_v25.json").load_config()
rest = ensure_shared_authenticated(creds)
ccy = cfg.currency_code
closed = 0
for _round in range(12):
    targets: list[tuple[str, str, float, str]] = []
    for item in rest.open_positions():
        market = item.get("market") or {}
        pos = item.get("position") or {}
        deal_id = str(pos.get("dealId") or "")
        side = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)
        epic = str(market.get("epic") or "")
        if deal_id and size > 0:
            targets.append((deal_id, side, size, epic))
    if not targets:
        break
    for deal_id, side, size, epic in targets:
        close_dir = "SELL" if side == "BUY" else "BUY"
        rest.close_position(
            deal_id,
            direction=close_dir,
            size=size,
            epic=epic or None,
            currency_code=ccy,
            verify=True,
        )
        closed += 1
        time.sleep(1.0)
print(f"Closed {closed} position(s)")
PY

# 2. Set allow_live_trading=false in config
CONFIG="${ROOT}/config/config_v25.json"
if [[ -f "$CONFIG" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    sed -i '' 's/"allow_live_trading"[[:space:]]*:[[:space:]]*true/"allow_live_trading": false/' "$CONFIG"
  else
    sed -i 's/"allow_live_trading"[[:space:]]*:[[:space:]]*true/"allow_live_trading": false/' "$CONFIG"
  fi
else
  echo "WARN: missing ${CONFIG}" >&2
fi

# 3. Stop self-healing watchdog (prevent auto-restart after emergency stop)
if command -v pgrep >/dev/null 2>&1; then
  while read -r wpid; do
    [[ -z "$wpid" ]] && continue
    kill -TERM "$wpid" 2>/dev/null || true
  done < <(pgrep -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true)
fi

# 4. Kill all agent processes (not this script)
SELF=$$
_kill_project_pids() {
  local sig=$1
  if ! command -v pgrep >/dev/null 2>&1; then
    return
  fi
  while read -r pid; do
    [[ -z "$pid" || "$pid" == "$SELF" ]] && continue
    local cmd
    cmd=$(ps -p "$pid" -o args= 2>/dev/null || true)
    [[ "$cmd" == *emergency_stop.sh* ]] && continue
    kill "$sig" "$pid" 2>/dev/null || true
  done < <(
    {
      pgrep -f "${ROOT}/src/main.py" 2>/dev/null || true
      pgrep -f "${ROOT}/launcher/IG Agent v25.app" 2>/dev/null || true
    } | sort -u
  )
}
_kill_project_pids TERM
sleep 1
_kill_project_pids KILL

# 5. Write emergency_stop.lock to project root
: > "${ROOT}/emergency_stop.lock"

echo "Emergency stop complete. Delete ${ROOT}/emergency_stop.lock before restart."
