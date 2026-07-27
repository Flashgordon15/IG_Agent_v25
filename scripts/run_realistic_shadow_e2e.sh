#!/usr/bin/env bash
# Realistic shadow E2E battery — twins untouched.
#
# Layers (most → least live-like, all offline / isolated):
#   1. Platform SIM E2E (signal → risk → dry exec → learning)
#   2. Dual-engine OHLC replay + train + metrics (real market bars)
#   3. In-process ShadowExecutor cycle (decision_engine → simulated fills → MTM)
#   4. Shadow loss loop on recent bleed day (LOGIC-only counterfactual)
#   5. Favour report → reports/realistic_shadow_e2e_YYYY-MM-DD.md
#
# Never POST /api/start|stop on :8080/:8081. Never lift A2. Never kill -9 mains.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

DAY="${1:-2026-07-24}"
SHADOW_ROOT="${ROOT}/src/data/v31-shadow-e2e"
REPORT_DIR="${ROOT}/src/data/v31-production/reports"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/realistic_shadow_e2e_${STAMP}.log"
PY="${ROOT}/.venv/bin/python3"
[[ -x "${PY}" ]] || PY="$(command -v python3)"

export APP_MODE="${APP_MODE:-DEMO}"
export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${ROOT}/src"
export IG_TEST_HARNESS=1
export IG_AGENT_PYTEST=1
# Isolated plane for shadow ledger / cycle — do not redirect production IG_DATA_ROOT
# for layers that must read live OHLC / autopsy; cycle script uses --data-root.

mkdir -p "${SHADOW_ROOT}" "${REPORT_DIR}" "${SHADOW_ROOT}/metrics" "${SHADOW_ROOT}/reports"

{
  echo "=== REALISTIC SHADOW E2E ${STAMP} day=${DAY} ==="
  echo "twins: DO NOT TOUCH"
  curl -s --max-time 3 http://127.0.0.1:8080/api/positions/live | "${PY}" -c "import sys,json;d=json.load(sys.stdin);print('CFD',d.get('count'),d.get('verdict'))" || echo "CFD unreachable"
  curl -s --max-time 3 http://127.0.0.1:8081/api/positions/live | "${PY}" -c "import sys,json;d=json.load(sys.stdin);print('SB',d.get('count'),d.get('verdict'))" || echo "SB unreachable"
} | tee "${LOG}"

echo "" | tee -a "${LOG}"
echo "=== L1 platform SIM E2E ===" | tee -a "${LOG}"
E2E_RC=0
IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH="${ROOT}/src" \
  "${PY}" scripts/e2e_platform_validation.py 2>&1 | tee -a "${LOG}" | tail -40 || E2E_RC=$?

echo "" | tee -a "${LOG}"
echo "=== L2 dual-engine replay learn (train+replay+metrics) ===" | tee -a "${LOG}"
ML_RC=0
IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH="${ROOT}/src" \
  "${PY}" scripts/ml_replay_learn.py all --limit 4000 2>&1 | tee -a "${LOG}" | tail -60 || ML_RC=$?

echo "" | tee -a "${LOG}"
echo "=== L3 ShadowExecutor in-process cycle ===" | tee -a "${LOG}"
CYCLE_RC=0
IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH="${ROOT}/src" \
  "${PY}" scripts/realistic_shadow_cycle.py \
    --data-root "${SHADOW_ROOT}" \
    --limit 800 \
    --day "${DAY}" \
    --write 2>&1 | tee -a "${LOG}" | tail -80 || CYCLE_RC=$?

echo "" | tee -a "${LOG}"
echo "=== L4 shadow loss loop day=${DAY} ===" | tee -a "${LOG}"
LOSS_RC=0
IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH="${ROOT}/src" \
  "${PY}" scripts/shadow_loss_loop.py --day "${DAY}" 2>&1 | tee -a "${LOG}" | tail -40 || LOSS_RC=$?

echo "" | tee -a "${LOG}"
echo "=== L5 favour synthesis ===" | tee -a "${LOG}"
FAV_RC=0
IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH="${ROOT}/src" \
  "${PY}" scripts/realistic_shadow_favour_report.py \
    --day "${DAY}" \
    --e2e-rc "${E2E_RC}" \
    --ml-rc "${ML_RC}" \
    --cycle-rc "${CYCLE_RC}" \
    --loss-rc "${LOSS_RC}" \
    --shadow-root "${SHADOW_ROOT}" \
    --log "${LOG}" \
    --write 2>&1 | tee -a "${LOG}" || FAV_RC=$?

echo "" | tee -a "${LOG}"
echo "=== twins re-check ===" | tee -a "${LOG}"
curl -s --max-time 3 http://127.0.0.1:8080/api/positions/live | "${PY}" -c "import sys,json;d=json.load(sys.stdin);print('CFD',d.get('count'),d.get('verdict'))" | tee -a "${LOG}"
curl -s --max-time 3 http://127.0.0.1:8081/api/positions/live | "${PY}" -c "import sys,json;d=json.load(sys.stdin);print('SB',d.get('count'),d.get('verdict'))" | tee -a "${LOG}"
curl -s --max-time 3 http://127.0.0.1:8080/api/health | "${PY}" -c "import sys,json;d=json.load(sys.stdin);print('CFD paused',d.get('trading_paused'))" | tee -a "${LOG}"
curl -s --max-time 3 http://127.0.0.1:8081/api/health | "${PY}" -c "import sys,json;d=json.load(sys.stdin);print('SB paused',d.get('trading_paused'))" | tee -a "${LOG}"

echo "Log: ${LOG}" | tee -a "${LOG}"
echo "Report: ${REPORT_DIR}/realistic_shadow_e2e_${DAY}.md" | tee -a "${LOG}"
exit 0
