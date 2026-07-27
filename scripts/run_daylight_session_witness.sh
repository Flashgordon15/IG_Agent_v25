#!/usr/bin/env bash
# Detach daylight London/US dual-desk witness (survives IDE/terminal teardown).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/detach_exec.sh
source "${ROOT}/scripts/lib/detach_exec.sh"

export IG_AGENT_CONFIG="${IG_AGENT_CONFIG:-config/config_v31_demo_throughput.json}"
export PYTHONPATH="${PYTHONPATH:-src}"
PY="${ROOT}/.venv/bin/python3"
[[ -x "${PY}" ]] || PY="$(command -v python3)"

STATE="${ROOT}/src/data/v31-production/state"
LOG_DIR="${ROOT}/src/data/v31-production/logs"
mkdir -p "${STATE}" "${LOG_DIR}"

DAY="$(TZ=Europe/London date +%Y-%m-%d)"
LAUNCH_LOG="${LOG_DIR}/daylight_witness_launch_${DAY}.log"
PID_FILE="${STATE}/daylight_session_witness.pid"
UNTIL="${DAYLIGHT_WITNESS_UNTIL:-16:00}"
POLL_SEC="${DAYLIGHT_WITNESS_POLL_SEC:-150}"

# Avoid duplicate witnesses
if [[ -f "${PID_FILE}" ]]; then
  old="$(tr -dc '0-9' <"${PID_FILE}" | head -c 12 || true)"
  if [[ -n "${old}" ]] && kill -0 "${old}" 2>/dev/null; then
    echo "daylight_session_witness already running pid=${old}"
    exit 0
  fi
fi

cd "${ROOT}"
detach_exec --log "${LAUNCH_LOG}" -- \
  env IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH=src \
  "${PY}" -u "${ROOT}/scripts/daylight_session_witness.py" \
  --until "${UNTIL}" --poll-sec "${POLL_SEC}"

echo "${DETACH_PID}" >"${PID_FILE}"
echo "detached daylight_session_witness pid=${DETACH_PID}"
echo "jsonl: ${LOG_DIR}/daylight_witness_${DAY}.jsonl"
echo "tail:  ${LOG_DIR}/daylight_witness_${DAY}.log"
echo "report: ${ROOT}/src/data/v31-production/reports/daylight_success_witness_${DAY}.md"
echo "until:  ${UNTIL} Europe/London (poll=${POLL_SEC}s)"
