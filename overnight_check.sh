#!/usr/bin/env bash
# Quick overnight readiness probe — five checks before development or bed.
# Usage: ./overnight_check.sh

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

pass=0
fail=0

check() {
  local label="$1"
  local ok="$2"
  local detail="${3:-}"
  if [ "$ok" = "1" ]; then
    echo "✓ $label${detail:+ — $detail}"
    pass=$((pass + 1))
  else
    echo "✗ $label${detail:+ — $detail}"
    fail=$((fail + 1))
  fi
}

echo ""
echo "IG Agent — OVERNIGHT CHECK"
echo "=========================="
echo ""

# 1. Git matches origin/main
git fetch origin -q 2>/dev/null || true
LOCAL_HEAD="$(git rev-parse HEAD 2>/dev/null || echo "")"
REMOTE_HEAD="$(git rev-parse origin/main 2>/dev/null || echo "")"
if [ -n "$LOCAL_HEAD" ] && [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
  check "Git sync with origin/main" 1 "$LOCAL_HEAD"
else
  check "Git sync with origin/main" 0 "local=${LOCAL_HEAD:-?} remote=${REMOTE_HEAD:-?}"
fi

# 2. Agent listening on 8080
LISTEN_PID="$(lsof -t -iTCP:8080 -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [ -n "$LISTEN_PID" ]; then
  check "Agent listening on :8080" 1 "PID $LISTEN_PID"
else
  check "Agent listening on :8080" 0 "no listener"
fi

# 3. Health API (watchdog User-Agent bypasses auth)
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "User-Agent: IG-Agent-Watchdog/" \
  http://127.0.0.1:8080/api/health)
if [ "$HTTP" = "200" ]; then
  check "Health API" 1 "HTTP $HTTP"
else
  check "Health API" 0 "HTTP $HTTP"
fi

# 4. Launchd supervision (watchdog + caffeinate)
WD_OK=0
CAFF_OK=0
UID_NUM="$(id -u)"
if launchctl print "gui/${UID_NUM}/com.igagent.v25.watchdog" >/dev/null 2>&1; then
  WD_OK=1
fi
if launchctl print "gui/${UID_NUM}/com.igagent.v25.caffeinate" >/dev/null 2>&1; then
  CAFF_OK=1
fi
if [ "$WD_OK" = "1" ] && [ "$CAFF_OK" = "1" ]; then
  check "Launchd supervision" 1 "watchdog + caffeinate loaded"
else
  check "Launchd supervision" 0 "watchdog=$WD_OK caffeinate=$CAFF_OK"
fi

# 5. No manual_stop hold
if [ ! -f "$ROOT/src/data/state/manual_stop.json" ]; then
  check "manual_stop absent" 1
else
  check "manual_stop absent" 0 "manual_stop.json present"
fi

echo ""
echo "=========================="
echo "Passed: $pass / 5"
echo ""

if [ "$fail" -eq 0 ]; then
  exit 0
fi
exit 1
