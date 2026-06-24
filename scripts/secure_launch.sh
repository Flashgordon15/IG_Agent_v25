#!/usr/bin/env bash
# Secure production launch — purge stale ports/PIDs, then exec main.py (LIVE).
#
# Usage:
#   ./scripts/secure_launch.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

echo "=== [1/4] INITIALISING IMPENETRABLE ENV PURGE ==="

# Engage hold during purge so launchd does not respawn mid-kill
PYTHONPATH=src "$PY" - <<'PY' 2>/dev/null || true
from system.shutdown_cleanup import mark_manual_stop
mark_manual_stop(source="secure_launch_purge")
PY

# 1. Forcefully clear relevant ports and agent instances (project-scoped)
lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -t -iTCP:49151 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -t -iTCP:3000 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
pgrep -f "${ROOT}/src/main.py" 2>/dev/null | xargs kill -9 2>/dev/null || true
killall -9 Python 2>/dev/null || true

sleep 1

# 2. Erase stray runtime file markers (not SQLite / learning history)
rm -f \
  src/data/state/alpha_matrix_publisher.pid \
  src/data/state/ui_terminal_3000.pid \
  src/data/.ig_agent_v29.lock \
  src/data/.ig_agent_v30_port_8080.lock \
  src/data/watchdog.pid
rm -f "${HOME}/.ig_agent_runtime.lock" "${HOME}/.ig_agent_v30_production.lock"
rm -rf /var/tmp/ig_agent* /tmp/ig_agent*

if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "ERROR: port 8080 still bound after purge — aborting launch." >&2
  exit 1
fi
if lsof -iTCP:49151 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "ERROR: port 49151 still bound after purge — aborting launch." >&2
  exit 1
fi

echo "=== [2/4] ENVIRONMENT SANITISED. CORE BOOTING... ==="

# Clear manual_stop so watchdog may supervise this session after bind
PYTHONPATH=src "$PY" - <<'PY' 2>/dev/null || true
from system.shutdown_cleanup import clear_manual_stop
clear_manual_stop()
PY

# 3. Absolute production export boundaries + non-blocking fast-bind boot
export IG_AGENT_ROOT="$ROOT"
export IG_AGENT_MODE=LIVE
export IG_AGENT_ALLOW_LIVE=1
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

echo "=== [3/4] PRODUCTION FLAGS ARMED (mode=LIVE non_blocking_boot=1) ==="
echo "=== [4/4] EXECUTING UNIFIED RUNTIME — foreground main.py ==="

exec "$PY" src/main.py
