#!/bin/bash
# Full agent soak test — run from project root after quitting any running session.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${ROOT}/src/data/logs/soak_test.log"
LAUNCHER="${ROOT}/launcher/IG Agent v25.app/Contents/MacOS/Launcher"
DURATION_SEC="${SOAK_DURATION_SEC:-180}"
POLL_SEC=5

mkdir -p "${ROOT}/src/data/logs"
: > "${LOG}"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${LOG}"
}

fail() {
  log "FAIL: $*"
  exit 1
}

log "=== soak test start (duration=${DURATION_SEC}s) ==="

# Preflight: port free
if lsof -i :8080 -sTCP:LISTEN >/dev/null 2>&1; then
  fail "port 8080 still in use — quit agent first"
fi

rm -f "${ROOT}/src/data/.ig_agent_v24.lock" "${ROOT}/src/data/.ig_agent_v25.lock"

log "launching via Desktop app stub"
"${LAUNCHER}" &
AGENT_PID=$!
sleep 3

# Wait for API
for i in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -sf "http://127.0.0.1:8080/health" >/dev/null || fail "health never came up"

log "health OK"

# Find main.py child
MAIN_PID=""
for _ in $(seq 1 30); do
  MAIN_PID=$(pgrep -f "${ROOT}/src/main.py" | head -1 || true)
  [ -n "${MAIN_PID}" ] && break
  sleep 1
done
[ -n "${MAIN_PID}" ] || fail "main.py never started"
log "main.py pid=${MAIN_PID}"

# Stream / market stream log line
for _ in $(seq 1 60); do
  if grep -q "market stream started" "${ROOT}/src/data/logs/engine.log" 2>/dev/null; then
    log "market stream started (log)"
    break
  fi
  sleep 1
done

STATE_CHANGES=0
PREEMPTIVE=0
HUB_FAIL=0
LAST_BID=""
WS_UPDATES=0

log "monitoring ${DURATION_SEC}s (poll=${POLL_SEC}s)"
END=$((SECONDS + DURATION_SEC))
while [ "${SECONDS}" -lt "${END}" ]; do
  if ! kill -0 "${MAIN_PID}" 2>/dev/null; then
    fail "main.py exited during soak"
  fi

  if curl -sf "http://127.0.0.1:8080/health" | grep -q '"ok":true'; then
  :
  else
    log "WARN: health not ok"
  fi

  BID=$(curl -sf "http://127.0.0.1:8080/state" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bid',''))" 2>/dev/null || echo "")
  if [ -n "${BID}" ] && [ "${BID}" != "${LAST_BID}" ] && [ -n "${LAST_BID}" ]; then
    STATE_CHANGES=$((STATE_CHANGES + 1))
  fi
  LAST_BID="${BID}"

  PREEMPTIVE=$((PREEMPTIVE + $(tail -200 "${ROOT}/src/data/logs/engine.log" 2>/dev/null | grep -c "preemptive_throttle" || true)))
  HUB_FAIL=$((HUB_FAIL + $(tail -200 "${ROOT}/src/data/logs/engine.log" 2>/dev/null | grep -c "MarketDataHub fetch failed" || true)))

  sleep "${POLL_SEC}"
done

# WebSocket update test
log "WebSocket burst test"
WS_OUT=$(PYTHONPATH=src python3 <<'PY' 2>/dev/null || echo "WS_FAIL"
import json, asyncio, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:8080/ws") as ws:
        await ws.recv()
        bids = []
        for _ in range(8):
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=4))
            bids.append(d.get("bid"))
        print("OK", len(set(bids)), bids[-3:])
asyncio.run(main())
PY
)
log "WebSocket: ${WS_OUT}"

# Second launcher (re-click) — agent must survive
log "simulating second Desktop click"
"${LAUNCHER}" &
sleep 3
if ! kill -0 "${MAIN_PID}" 2>/dev/null; then
  fail "main.py died on second launcher click"
fi
NEW_PID=$(pgrep -f "${ROOT}/src/main.py" | head -1 || true)
log "after re-click main pid=${NEW_PID} (was ${MAIN_PID})"

# API smoke
curl -sf "http://127.0.0.1:8080/api/system" >/dev/null && log "GET /api/system OK" || log "WARN /api/system"
curl -sf -X POST "http://127.0.0.1:8080/api/start" | grep -q '"ok"' && log "POST /api/start OK" || log "WARN /api/start"

# Final state
curl -sf "http://127.0.0.1:8080/state" | python3 -c "
import sys,json
d=json.load(sys.stdin)
h=d.get('health',{})
print('final bid', d.get('bid'), 'stream', d.get('stream_status'), 'tick_age', d.get('tick_age_s'))
print('badge', h.get('badge'), 'summary', h.get('summary', '')[:80])
" | tee -a "${LOG}"

log "state price changes during soak: ${STATE_CHANGES}"
log "engine log lines (tail) preemptive_throttle count (approx): ${PREEMPTIVE}"
log "MarketDataHub fetch failed count (approx): ${HUB_FAIL}"

# Shutdown — prefer instance lock PID (main.py) over launcher child
SHUTDOWN_PID="${MAIN_PID}"
LOCK_FILE="${ROOT}/src/data/.ig_agent_v25.lock"
if [ -f "${LOCK_FILE}" ]; then
  LOCK_PID=$(head -1 "${LOCK_FILE}" 2>/dev/null | awk '{print $1}' || true)
  if [ -n "${LOCK_PID}" ] && kill -0 "${LOCK_PID}" 2>/dev/null; then
    SHUTDOWN_PID="${LOCK_PID}"
  fi
fi
log "sending SIGTERM to pid=${SHUTDOWN_PID}"
kill -TERM "${SHUTDOWN_PID}" 2>/dev/null || true
sleep 5
if kill -0 "${SHUTDOWN_PID}" 2>/dev/null; then
  kill -KILL "${SHUTDOWN_PID}" 2>/dev/null || true
fi
if lsof -i :8080 -sTCP:LISTEN >/dev/null 2>&1; then
  log "WARN: port 8080 still listening after shutdown"
fi

log "step 6 — session flatten stress (mock, no IG)"
if PYTHONPATH=src python3 -m pytest tests/test_session_flatten_stress.py -q >>"${LOG}" 2>&1; then
  log "flatten stress test PASS"
else
  log "WARN: flatten stress test failed — see ${LOG}"
fi

log "=== soak test complete ==="
log "Full plan: docs/STRESS_TEST_PLAN.md"
