#!/usr/bin/env bash
# Detach overnight dual-desk monitor (survives IDE/terminal teardown).
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
LAUNCH_LOG="${LOG_DIR}/overnight_monitor_launch_${DAY}.log"
PID_FILE="${STATE}/overnight_desk_monitor.pid"

# Avoid duplicate monitors
if [[ -f "${PID_FILE}" ]]; then
  old="$(tr -dc '0-9' <"${PID_FILE}" | head -c 12 || true)"
  if [[ -n "${old}" ]] && kill -0 "${old}" 2>/dev/null; then
    echo "overnight_desk_monitor already running pid=${old}"
    exit 0
  fi
fi

cd "${ROOT}"
detach_exec --log "${LAUNCH_LOG}" -- \
  env IG_AGENT_CONFIG="${IG_AGENT_CONFIG}" PYTHONPATH=src \
  "${PY}" -u "${ROOT}/scripts/overnight_desk_monitor.py" --poll-sec 180

echo "${DETACH_PID}" >"${PID_FILE}"
echo "detached overnight_desk_monitor pid=${DETACH_PID}"
echo "jsonl: ${LOG_DIR}/overnight_monitor_${DAY}.jsonl"
echo "tail:  ${LOG_DIR}/overnight_monitor_${DAY}.log"
echo "report target: ${ROOT}/src/data/v31-production/reports/trading_report_2026-07-24_0800.md"
