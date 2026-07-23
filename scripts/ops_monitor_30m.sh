#!/usr/bin/env bash
# 30-minute ops monitor — health, gates, errors. Logs to src/data/logs/ops_monitor.log
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${ROOT}/src/data/logs/ops_monitor.log"
# shellcheck source=lib/detach_exec.sh
source "${ROOT}/scripts/lib/detach_exec.sh"
PY="${ROOT}/.venv/bin/python3"
END=$((SECONDS + 1800))

mkdir -p "${ROOT}/src/data/logs"

log() { printf '%s | %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*" | tee -a "${LOG}"; }

log "=== ops monitor start (30m) ==="

while (( SECONDS < END )); do
  if ! pgrep -f "${ROOT}/src/main.py" >/dev/null 2>&1; then
    log "ALERT agent PID missing — attempting headless restart"
    if [[ -f "${ROOT}/config/credentials/launch.env" ]]; then
      set -a
      # shellcheck disable=SC1091
      source "${ROOT}/config/credentials/launch.env"
      set +a
    fi
    export IG_AGENT_ROOT="${ROOT}"
    export IG_AGENT_FROM_LAUNCHER=1
    export IG_AGENT_SKIP_DEPLOY_CHECK=1
    export PYTHONPATH="${ROOT}/src"
    detach_exec --log "${ROOT}/src/data/logs/agent_restart.log" -- \
      caffeinate -i -s bash "${ROOT}/scripts/start_agent_background.sh"
    sleep 30
    continue
  fi

  health="$(curl -sf --max-time 3 http://127.0.0.1:8080/health 2>/dev/null || echo DOWN)"
  cockpit="$(curl -sf --max-time 3 http://127.0.0.1:8787/api/health 2>/dev/null || echo DOWN)"

  snap="$("${PY}" - <<'PY' 2>/dev/null || echo '{}'
import json, os, sys
sys.path.insert(0, os.environ.get("ROOT_SRC", "src"))
try:
    from api.auth import issue_session_token
    import urllib.request
    token = issue_session_token()
    req = urllib.request.Request(
        "http://127.0.0.1:8080/api/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=4) as r:
        d = json.load(r)
    print(json.dumps({
        "ok": d.get("ok"),
        "trading_healthy": d.get("trading_healthy"),
        "quotes": f"{d.get('quotes_fresh_count')}/{d.get('quotes_total')}",
        "stream": d.get("stream_status"),
        "rest_min": d.get("rest_calls_min"),
    }))
except Exception as e:
    print(json.dumps({"error": type(e).__name__}))
PY
)"

  err_tail="$(grep -E 'CRITICAL|FATAL|Gate[0-9] FATAL|authentication failed' \
    "${ROOT}/src/data/logs/engine.log" 2>/dev/null | tail -1 || true)"

  log "health=${health} cockpit=${cockpit} snap=${snap} last_err=${err_tail:-none}"

  sleep 120
done

log "=== ops monitor complete ==="
