#!/bin/bash
# IG Agent v41 — macOS one-click launcher (delegates to native supervisor).
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${LAUNCHER_DIR}/../.." && pwd)"
cd "${ROOT}"

if [[ -x "${LAUNCHER_DIR}/IGAgentSupervisor" ]]; then
  exec "${LAUNCHER_DIR}/IGAgentSupervisor"
fi
if [[ -x "${LAUNCHER_DIR}/igagent_launcher" ]]; then
  exec "${LAUNCHER_DIR}/igagent_launcher"
fi

exec /bin/bash "${LAUNCHER_DIR}/igagent_launcher.sh"
