#!/bin/bash
# IG Agent v29 — System Health Check
# Run before every trading session and after every restart

set -e
AGENT_URL="http://127.0.0.1:8080"
HEADER="User-Agent: IG-Agent-Watchdog/"
PASS=0
FAIL=0
WARN=0

check() {
    local desc="$1"
    local result="$2"
    local expected="$3"
    if [ "$result" = "$expected" ]; then
        echo "[PASS] $desc"
        PASS=$((PASS+1))
    else
        echo "[FAIL] $desc — got: $result expected: $expected"
        FAIL=$((FAIL+1))
    fi
}

warn() {
    echo "[WARN] $1"
    WARN=$((WARN+1))
}

echo "================================"
echo "IG AGENT HEALTH CHECK"
echo "$(TZ='Europe/London' date '+%H:%M:%S BST %a %d %b %Y')"
echo "================================"

HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "[INFO] Version: $HASH"

LOCK="src/data/.ig_agent_v29.lock"
if [ -f "$LOCK" ]; then
    LOCK_PID=$(cat "$LOCK" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[PASS] Lock file: PID $LOCK_PID alive"
        PASS=$((PASS+1))
    else
        warn "Stale lock file — PID ${LOCK_PID:-?} dead"
    fi
fi

PID=$(lsof -ti:8080 2>/dev/null || true)
check "Agent on :8080" "$([ -n "$PID" ] && echo yes || echo no)" "yes"
echo "[INFO] PID: ${PID:-none}"

HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "$HEADER" "$AGENT_URL/api/health" 2>/dev/null || echo "000")
check "Health endpoint" "$HTTP" "200"

if [ "$HTTP" = "200" ]; then
    STATE=$(curl -s -H "$HEADER" "$AGENT_URL/state" 2>/dev/null || echo "{}")
    HEALTHY=$(echo "$STATE" | python3 -c \
        "import json,sys; d=json.load(sys.stdin); print(str(d.get('trading_healthy',False)).lower())" \
        2>/dev/null || echo "false")
    check "trading_healthy" "$HEALTHY" "true"

    LIVE_COUNT=$(echo "$STATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
markets = d.get('markets', {})
enabled = ['CS.D.CFPGOLD.CFP.IP','CS.D.EURUSD.CFD.IP',
           'CS.D.GBPUSD.CFD.IP','IX.D.NIKKEI.IFM.IP',
           'IX.D.DOW.IFM.IP','IX.D.NASDAQ.IFM.IP']
live = sum(1 for e in enabled
           if markets.get(e,{}).get('stream_status')=='LIVE')
print(live)
" 2>/dev/null || echo "0")
    echo "[INFO] Enabled epics LIVE: $LIVE_COUNT/6"
    if [ "$LIVE_COUNT" = "6" ]; then
        echo "[PASS] All 6 enabled epics LIVE"
        PASS=$((PASS+1))
    else
        warn "Only $LIVE_COUNT/6 enabled epics LIVE"
    fi

    REST_PCT=$(echo "$STATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('rest_budget_pct', 'unknown'))
" 2>/dev/null || echo "unknown")
    echo "[INFO] REST budget remaining: ${REST_PCT}%"

    POS=$(echo "$STATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(len(d.get('positions',[]) or []))
" 2>/dev/null || echo "?")
    echo "[INFO] Open positions: $POS"

    BST_TIME=$(curl -s -H "$HEADER" "$AGENT_URL/api/time" \
        2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"{d.get('bst','?')} {d.get('session','?')}\")
" 2>/dev/null || echo "?")
    echo "[INFO] Agent BST: $BST_TIME"

    STALL=$(echo "$STATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(str(d.get('rest_poll_stalled',False)).lower())
" 2>/dev/null || echo "unknown")
    if [ "$STALL" = "false" ]; then
        echo "[PASS] REST poll: active"
        PASS=$((PASS+1))
    else
        warn "REST poll stalled: $STALL"
    fi

    ML=$(curl -s -H "$HEADER" \
        "$AGENT_URL/api/stats/edge-analysis" \
        2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
ml=d.get('ml_readiness',{})
print(f\"{ml.get('confirmed_live_trades',0)}/50\")
" 2>/dev/null || echo "?/?")
    echo "[INFO] ML readiness: $ML clean trades"
fi

CAF=$(pgrep caffeinate 2>/dev/null || true)
check "Caffeinate" "$([ -n "$CAF" ] && echo yes || echo no)" "yes"

WATCH=$(launchctl list 2>/dev/null | grep -i igagent | head -1 || true)
check "Launchd watchdog" "$([ -n "$WATCH" ] && echo yes || echo no)" "yes"

MSTOP="src/data/state/manual_stop.json"
check "Manual stop absent" \
    "$([ ! -f "$MSTOP" ] && echo yes || echo no)" "yes"

echo "================================"
echo "RESULT: $PASS PASS | $WARN WARN | $FAIL FAIL"
if [ "$FAIL" -eq 0 ]; then
    echo "STATUS: HEALTHY"
else
    echo "STATUS: ISSUES FOUND — review above"
fi
echo "================================"
