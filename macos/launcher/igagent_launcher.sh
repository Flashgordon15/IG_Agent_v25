#!/bin/bash
# Shell supervisor — same contract as igagent_launcher (Go). Used when binary not built.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export IG_AGENT_ROOT="${IG_AGENT_ROOT:-${ROOT}}"

# shellcheck source=lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

LOG="${IG_AGENT_ROOT}/logs/igagent_launcher.log"
mkdir -p "${IG_AGENT_ROOT}/logs"
exec > >(tee -a "${LOG}") 2>&1

echo "========== igagent_launcher.sh $(date -u +%Y-%m-%dT%H:%M:%SZ) =========="
notify_launcher "IG Agent" "Clean launch starting…"

run_step() {
  local script="$1"
  local required="${2:-1}"
  echo "==> ${script}"
  if ! /bin/bash "${SCRIPT_DIR}/${script}"; then
    if [[ "${required}" == "1" ]]; then
      alert_launcher "Launch failed at ${script}"
      exit 1
    fi
    echo "WARN: optional step ${script} failed"
  fi
}

run_step agent_kill.sh 1
run_step agent_start.sh 1
run_step agent_verify.sh 1
run_step agent_gui.sh 0

notify_launcher "IG Agent" "Agent ready."
echo "✅ igagent_launcher.sh complete — http://127.0.0.1:${IG_API_PORT:-8080}/"
exit 0
