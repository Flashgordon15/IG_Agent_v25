#!/usr/bin/env bash
# Emit agent loop tick with compact IG Agent status snapshot.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API="http://127.0.0.1:8080/state"
HEALTH="http://127.0.0.1:8080/health"

alive="no"
summary="OFFLINE"
if curl -sf --max-time 5 "${HEALTH}" >/dev/null 2>&1; then
  alive="yes"
  summary="$(PYTHONPATH="${ROOT}/src" python3 - "$API" <<'PY' 2>/dev/null || echo "BAD_STATE"
import json, sys, urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        d = json.load(r)
except Exception as e:
    print(f"FETCH_ERROR: {e}")
    raise SystemExit(0)
h = d.get("health") or {}
gates = h.get("gates") or []
passing = sum(1 for g in gates if g.get("pass"))
total = len(gates) or 7
sig = next((g for g in gates if g.get("name") == "signal_confidence"), {})
s = d.get("signal") or {}
pts = d.get("points") or {}
print(
    f"alive=yes | market={d.get('market_state')} stream={d.get('stream_status')} "
    f"tick_age={d.get('tick_age_s')}s | gates={passing}/{total} badge={h.get('badge')} | "
    f"signal={s.get('direction')} raw={s.get('raw_direction')} conf={s.get('confidence')} fitness={s.get('fitness')} | "
    f"bid={d.get('bid')} offer={d.get('offer')} spread={d.get('spread')} | "
    f"positions={len(d.get('positions') or [])} daily_pnl={d.get('daily_pnl_gbp')} balance={d.get('balance_gbp')} | "
    f"points={pts.get('state')} cum={pts.get('cumulative')} | {h.get('summary', '')[:120]}"
)
PY
)"
fi

PROMPT=$(cat <<EOF
Monitor IG Agent v25 performance. Read-only unless I say fix.
Compare to prior check if known. Report: process up, gates, signal, P&L, positions, errors, maintenance, OHLC/cache, notable engine.log lines (tail 15).
Snapshot: ${summary}
EOF
)

# JSON-escape prompt for single-line payload
python3 -c 'import json,sys; print(json.dumps({"prompt": sys.stdin.read()}))' <<<"$PROMPT" | \
  sed "s/^/AGENT_LOOP_TICK_ig_agent_monitor /"
