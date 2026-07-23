#!/usr/bin/env bash
# Application Stability Harness — operator one-shot status.
# Observe-only by default. Pass --act to allow safe peripheral heals.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH=src

PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

# Prefer live API when agent is up (same composite the Terminal sees).
if curl -sf --max-time 2 "http://127.0.0.1:8080/api/desk/stability" >/tmp/desk_stability_$$.json 2>/dev/null; then
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import json
from pathlib import Path
p = sorted(Path("/tmp").glob("desk_stability_*.json"))[-1]
d = json.loads(p.read_text())
ds = d.get("desk_stability") or d
print(f"grade={ds.get('grade')}  {ds.get('label')}")
for r in ds.get("reasons") or []:
    print(f"  - {r}")
c = ds.get("components") or {}
print(
    f"  path_live={c.get('trading_path_live')} sot_ok={c.get('sot_ok')} "
    f"ui={c.get('ui_up')} rest={c.get('rest_pressure_level')} opens={c.get('broker_open')}"
)
feed = c.get("feed_transport") or {}
if feed.get("label"):
    print(f"  feed: {feed.get('label')}")
PY
  else
    cat /tmp/desk_stability_$$.json
  fi
  rm -f /tmp/desk_stability_$$.json
  exit 0
fi

exec "$PY" -m runtime.desk_stability_harness --once "$@"
