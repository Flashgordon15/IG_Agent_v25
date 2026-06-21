#!/usr/bin/env bash
# Open the live Vanguard dashboard (:8080) — respawns live track if API is down.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH_URL="http://127.0.0.1:8080/api/health"
DASHBOARD_URL="http://127.0.0.1:8080/?launch=$(date +%s)"
COCKPIT_URL="http://127.0.0.1:8787/"

notify() {
  local msg="$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${msg}\" with title \"IG Agent Live\"" 2>/dev/null || true
  fi
}

alert_fail() {
  local msg="$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"IG Agent Live\" message \"${msg}\" as warning" 2>/dev/null || true
  fi
  echo "ERROR: ${msg}" >&2
}

live_healthy() {
  curl -sf --max-time 3 -H "User-Agent: IG-Agent-Watchdog/1.0" "${HEALTH_URL}" >/dev/null 2>&1
}

dashboard_html_ok() {
  local body=""
  body="$(curl -sf --max-time 3 "${DASHBOARD_URL%%\?*}/" 2>/dev/null)" || return 1
  [[ "${body}" == *"<html"* ]] || [[ "${body}" == *"<!doctype"* ]]
}

if ! live_healthy; then
  echo "Live Vanguard not healthy — respawning…"
  "${ROOT}/scripts/respawn_live_vanguard.sh" || alert_fail "Could not respawn Live Vanguard on :8080"
fi

if ! live_healthy; then
  alert_fail "Live agent is not responding on :8080. Check /tmp/ig_agent.live.log"
  exit 1
fi

if ! dashboard_html_ok; then
  alert_fail "Dashboard HTML not served on :8080 — rebuild dashboard/dist and retry"
  exit 1
fi

if command -v open >/dev/null 2>&1; then
  open -g "${DASHBOARD_URL}" 2>/dev/null || open "${DASHBOARD_URL}"
  notify "Live dashboard opened — sign in with IG_PASSWORD from .env"
  echo "Opened: ${DASHBOARD_URL}"
  echo "Flight Deck (read-only): ${COCKPIT_URL}"
else
  echo "${DASHBOARD_URL}"
fi
