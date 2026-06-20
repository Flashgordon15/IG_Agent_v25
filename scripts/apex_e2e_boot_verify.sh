#!/usr/bin/env bash
# Apex v30 shadow E2E boot verifier — purge, boot sidecar, poll until ready.
set -euo pipefail

RUN_LABEL="${1:-run}"
AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ISOLATED_ROOT="${HOME}/Library/Application Support/IG Agent Apex/v30-production"
LOG_FILE="/tmp/apex_e2e_${RUN_LABEL}.log"
PID_FILE="/tmp/apex_e2e_${RUN_LABEL}.pid"

cd "${AGENT_DIR}"

echo "=== E2E ${RUN_LABEL}: purge ==="
bash scripts/apex-shadow-purge.sh
rm -f "${ISOLATED_ROOT}/data/.ig_agent_v30_shadow.lock" 2>/dev/null || true
rm -f "${ISOLATED_ROOT}/data/apex_ipc.sock" 2>/dev/null || true
mkdir -p "${ISOLATED_ROOT}/data/logs" "${ISOLATED_ROOT}/data/state" "${ISOLATED_ROOT}/analytics"

# Never touch production :8080
export NODE_ENV=shadow
export IG_NODE_PROFILE=shadow
export IG_APEX_DESKTOP=1
export IG_APEX_NO_BROWSER=1
export IG_APEX_PROTECT_PRODUCTION_PORTS=1
export IG_AGENT_SKIP_ORPHAN_KILL=1
export IG_AGENT_FROM_LAUNCHER=1
export IG_API_PORT=9090
export IG_COCKPIT_PORT=9191
export IG_AGENT_DATA_DIR="${ISOLATED_ROOT}/data"
export IG_ANALYTICS_DB="${ISOLATED_ROOT}/analytics/triage_v30.db"
export IG_TRIAGE_DB="${ISOLATED_ROOT}/analytics/triage_v30.db"
export PYTHONPATH="${AGENT_DIR}/src"

# Shadow-only: never pkill production :8080 main.py
if command -v lsof >/dev/null 2>&1; then
  for pid in $(lsof -tiTCP:9090 -sTCP:LISTEN 2>/dev/null || true); do
    kill -TERM "${pid}" 2>/dev/null || true
  done
  sleep 1
fi

echo "=== E2E ${RUN_LABEL}: start sidecar ==="
"${AGENT_DIR}/.venv/bin/python3" "${AGENT_DIR}/src/main.py" >"${LOG_FILE}" 2>&1 &
echo $! >"${PID_FILE}"
PID=$(cat "${PID_FILE}")

cleanup() {
  if kill -0 "${PID}" 2>/dev/null; then
    kill -TERM "${PID}" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "${PID}" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "${PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "=== E2E ${RUN_LABEL}: poll :9090 (max 300s) ==="
READY=0
for i in $(seq 1 150); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:9090/api/health" 2>/dev/null || echo "000")
  if [[ "${CODE}" == "200" ]]; then
    BODY=$(curl -s "http://127.0.0.1:9090/api/startup/status" 2>/dev/null || echo "{}")
    if [[ "${BODY}" == *'"ready":true'* ]] || [[ "${BODY}" == *'"ready": true'* ]]; then
      READY=1
      echo "READY at t=$((i * 2))s health=${CODE}"
      echo "${BODY}" | head -c 400
      echo
      break
    fi
    echo "t=$((i * 2))s health=${CODE} boot_in_progress"
  else
    echo "t=$((i * 2))s health=${CODE}"
  fi
  sleep 2
done

if [[ "${READY}" != "1" ]]; then
  echo "FAIL: sidecar not ready"
  tail -40 "${LOG_FILE}"
  exit 1
fi

echo "=== E2E ${RUN_LABEL}: PASS ==="
exit 0
