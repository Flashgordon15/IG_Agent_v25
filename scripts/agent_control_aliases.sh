#!/usr/bin/env bash
# IG Agent v30 — institutional parallel track control aliases
# Source from your shell profile:
#   source /Users/chrisgordon/Projects/IG_Agent_v25/scripts/agent_control_aliases.sh

agent-status() {
  echo "=== IG Agent Parallel Track Status ==="
  if [[ -f /tmp/ig_agent_parallel.pids.json ]]; then
    python3 - <<'PY'
import json
from pathlib import Path
reg = json.loads(Path("/tmp/ig_agent_parallel.pids.json").read_text())
print("registry:", json.dumps(reg, indent=2))
for label, key in (("Live Vanguard", "live_pid"), ("Shadow Simulator", "shadow_pid")):
    pid = reg.get(key)
    if pid:
        print(f"{label}: pid={pid}")
PY
  else
    echo "registry: (missing /tmp/ig_agent_parallel.pids.json)"
  fi
  echo "--- ports ---"
  lsof -iTCP:8080 -sTCP:LISTEN 2>/dev/null || echo ":8080 not listening"
  lsof -iTCP:9199 -sTCP:LISTEN 2>/dev/null || echo ":9199 not listening"
  echo "--- locks ---"
  for port in 8080 9199; do
    find "$HOME/Library/Application Support/IG Agent Apex" -name ".ig_agent_v30_port_${port}.lock" 2>/dev/null
  done
  echo "--- shared memory ---"
  PYTHONPATH="${IG_AGENT_ROOT:-$PWD}/src" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH", "src"))
try:
    from system.identity.shared_memory_bridge import attach_shared_memory_consumer, shm_name_for_track
    for key in ("live", "shadow"):
        b = attach_shared_memory_consumer(track=key)
        print(f"{shm_name_for_track(key)}:", "OK" if b.is_initialized() else "EMPTY", "size=", b.size)
except Exception as exc:
    print("track_state:", "ERROR", exc)
try:
    from system.identity.weight_transfer_bridge import get_weight_transfer_bridge
    w = get_weight_transfer_bridge(create=False)
    print("weight_xfer:", "OK" if w.is_initialized() else "EMPTY")
except Exception as exc:
    print("weight_xfer:", "ERROR", exc)
PY
}

agent-logs() {
  tail -f /tmp/ig_agent.live.log
}

agent-kill-all() {
  PYTHONPATH="${IG_AGENT_ROOT:-$PWD}/src" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH", "src"))
from system.identity.process_orchestrator import emergency_kill_all_tracks
print(emergency_kill_all_tracks())
PY
  kill -TERM $(lsof -t -i:8080 -i:9199 2>/dev/null) 2>/dev/null || true
  echo "agent-kill-all: SIGTERM issued, locks cleared, shared RAM wiped"
}
