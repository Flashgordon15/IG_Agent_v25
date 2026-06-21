#!/usr/bin/env bash
# Respawn Live Vanguard on :8080 when flat — never touches shadow :9199.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH_URL="http://127.0.0.1:8080/api/health"
LIVE_LOG="/tmp/ig_agent.live.log"
CYCLE_SEC="${IG_DAEMON_CYCLE_SEC:-900}"

live_listening() {
  curl -sf --max-time 3 -H "User-Agent: IG-Agent-Watchdog/1.0" "${HEALTH_URL}" >/dev/null 2>&1
}

echo "[respawn] checking Live Vanguard on :8080…"

if live_listening; then
  echo "[respawn] Live Vanguard already healthy on :8080"
  exit 0
fi

echo "[respawn] :8080 not healthy — clearing stale live track PIDs (SIGTERM only)"

for pid in $(pgrep -f "${ROOT}/src/main.py --isolated-track=live" 2>/dev/null || true); do
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "[respawn] SIGTERM stale live pid=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
  fi
done

for _ in $(seq 1 30); do
  if ! pgrep -f "${ROOT}/src/main.py --isolated-track=live" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "[respawn] ERROR: :8080 still bound after live SIGTERM — operator action required" >&2
  exit 1
fi

echo "[respawn] spawning isolated live track (cycle=${CYCLE_SEC}s)…"

cd "${ROOT}"
export PYTHONPATH="${ROOT}/src"
export IG_APEX_PROTECT_PRODUCTION_PORTS=1

NEW_PID="$("${ROOT}/.venv/bin/python3" - <<'PY'
from pathlib import Path
from system.env_loader import prepare_boot_env
from system.identity.process_orchestrator import read_pid_registry, spawn_isolated_track, write_pid_registry

prepare_boot_env()
proc = spawn_isolated_track(
    track="live",
    cycle_sec=int(__import__("os").environ.get("IG_DAEMON_CYCLE_SEC", "900")),
    log_path=Path("/tmp/ig_agent.live.log"),
)
reg = read_pid_registry()
write_pid_registry(
    live_pid=proc.pid,
    shadow_pid=int(reg.get("shadow_pid") or 0),
    orchestrator_pid=int(reg.get("orchestrator_pid") or 0),
    cockpit_pid=int(reg.get("cockpit_pid") or 0) if reg.get("cockpit_pid") else None,
)
print(proc.pid)
PY
)"

echo "[respawn] live spawn pid=${NEW_PID} — waiting for Gate5 READY (max 180s)"

for _ in $(seq 1 90); do
  if live_listening; then
    echo "[respawn] Live Vanguard READY on :8080 (pid=${NEW_PID})"
    exit 0
  fi
  sleep 2
done

echo "[respawn] ERROR: live track did not reach healthy state — see ${LIVE_LOG}" >&2
exit 1
