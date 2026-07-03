#!/bin/bash
# Iron Cage Flight Deck — native desktop shell entry (pywebview).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_lib.sh
source "${SCRIPT_DIR}/agent_lib.sh"

export_launch_env
export IG_DESKTOP_FLIGHT_DECK=1
export IG_DESKTOP_SHELL_ACTIVE=1
export IG_COCKPIT_URL="${IG_COCKPIT_URL:-http://127.0.0.1:8787/}"

# Cold-start: socket audit + purge zombies on 8080/8787 before WKWebView binds
log "desktop_flight_deck: cold-start port audit (8080, 8787)"
"${IG_AGENT_PY}" - <<'PY' || true
import json
import os
from cockpit.desktop_process_guard import audit_and_purge_bound_ports

summary = audit_and_purge_bound_ports()
print(json.dumps(summary))
log_path = os.path.join(os.environ.get("IG_AGENT_ROOT", "."), "logs", "desktop_port_audit.json")
os.makedirs(os.path.dirname(log_path), exist_ok=True)
with open(log_path, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2)
PY

ARGS=(--cockpit-url "${IG_COCKPIT_URL}")
if [[ "${LAUNCHER_DESKTOP:-}" == "1" ]]; then
  ARGS+=(--launch-supervisor)
fi
for arg in "$@"; do
  if [[ "${arg}" == "--launch-supervisor" ]]; then
    if [[ " ${ARGS[*]} " != *" --launch-supervisor "* ]]; then
      ARGS+=(--launch-supervisor)
    fi
  fi
done

exec "${IG_AGENT_PY}" -m cockpit.desktop_app_shell "${ARGS[@]}"
