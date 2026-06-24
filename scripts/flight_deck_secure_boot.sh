#!/usr/bin/env bash
# Flight Deck Integrity Shield — forensic preflight + production launch.
#
# Usage:
#   ./scripts/flight_deck_secure_boot.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

echo "=== [0/5] INTEGRITY SHIELD FORENSIC PREFLIGHT ==="

PYTHONPATH=src "$PY" - <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path.cwd()

main_py = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
cache_py = (ROOT / "src" / "trading" / "cache_reaper.py").read_text(encoding="utf-8")
errors: list[str] = []

if "SO_REUSEADDR" not in main_py:
    errors.append("main.py missing SO_REUSEADDR on singleton socket")
if "enforce_absolute_socket_singleton" not in main_py:
    errors.append("main.py missing enforce_absolute_socket_singleton()")

# Server initialization anchor — no synchronous outbound network before fast-bind path.
anchor = main_py.find("def _run_immutable_fast_bind_server")
if anchor < 0:
    anchor = main_py.find("uvicorn.Server")
boot_slice = main_py[:anchor] if anchor > 0 else main_py[:8000]
blocked_patterns = (
    r"urllib\.request\.urlopen\s*\(",
    r"requests\.(get|post|put|delete)\s*\(",
    r"httpx\.(get|post|Client)\s*\(",
    r"IgRestClient\s*\(",
    r"rest_client\.[a-z_]+\s*\(",
)
for pat in blocked_patterns:
    if re.search(pat, boot_slice):
        errors.append(f"blocking/sync network pattern before server init: {pat}")

if "RING_GOVERNOR_MAX_SLOTS" not in cache_py or "50000" not in cache_py.replace("_", ""):
    if "50_000" not in cache_py:
        errors.append("cache_reaper missing 50_000 slot ring governor")
if "govern_live_tick_ingest" not in cache_py:
    errors.append("cache_reaper missing govern_live_tick_ingest FIFO governor")
if "volatile_runtime_state_set" not in cache_py:
    errors.append("cache_reaper missing volatile RAM runtime mirror")
if re.search(r"def tick_once[\s\S]*?_atomic_write_json", cache_py):
    errors.append("cache_reaper tick_once must not write disk on hot path")

if errors:
    print("INTEGRITY SHIELD FAIL — structural drift detected:", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    sys.exit(1)

print("INTEGRITY SHIELD OK — main.py socket macro + RAM reaper contract verified")
PY

echo "=== [1/5] PROCESS TREE PURGE ==="

PYTHONPATH=src "$PY" - <<'PY' 2>/dev/null || true
from system.shutdown_cleanup import mark_manual_stop
mark_manual_stop(source="flight_deck_secure_boot")
PY

lsof -t -i:49151 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -t -i:8080 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -t -i:3000 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
pgrep -f "${ROOT}/src/main.py" 2>/dev/null | xargs kill -9 2>/dev/null || true
killall -9 python python3 Python 2>/dev/null || true

sleep 1

rm -f \
  src/data/state/alpha_matrix_publisher.pid \
  src/data/state/ui_terminal_3000.pid \
  src/data/.ig_agent_v29.lock \
  src/data/.ig_agent_v30_port_8080.lock \
  src/data/watchdog.pid
rm -f "${HOME}/.ig_agent_runtime.lock" "${HOME}/.ig_agent_v30_production.lock"

if lsof -iTCP:49151 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "ERROR: port 49151 still bound — aborting." >&2
  exit 1
fi
if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "ERROR: port 8080 still bound — aborting." >&2
  exit 1
fi

echo "=== [2/5] ENVIRONMENT SANITISED ==="

PYTHONPATH=src "$PY" - <<'PY' 2>/dev/null || true
from system.shutdown_cleanup import clear_manual_stop
clear_manual_stop()
PY

export IG_AGENT_ROOT="$ROOT"
export IG_AGENT_MODE=LIVE
export IG_AGENT_ALLOW_LIVE=1
export IG_MICRO_LOT_VERIFY=1
export IG_NON_BLOCKING_BOOT=1
export IG_AGENT_FROM_LAUNCHER=1
export PYTHONPATH="${ROOT}/src"

LAUNCH_ENV="${ROOT}/config/credentials/launch.env"
if [[ -f "${LAUNCH_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${LAUNCH_ENV}"
  set +a
fi

echo "=== [3/5] CHAOS MATRIX CONTRACT (optional regen) ==="
if [[ -f "${ROOT}/scripts/generate_chaos_matrix.py" ]]; then
  "$PY" "${ROOT}/scripts/generate_chaos_matrix.py" || true
fi

echo "=== [4/5] PRODUCTION FLAGS ARMED ==="
echo "=== [5/5] EXECUTING FLIGHT DECK LIVE ENGINE ==="

exec "$PY" src/main.py
