#!/usr/bin/env bash
# Pre-launch checks for IG Agent Apex shadow desktop (:9090 only).
set -euo pipefail

APP="${1:-$(cd "$(dirname "$0")/.." && pwd)/dist-apex/mac-arm64/IG Agent Apex.app}"
HEALTH="http://127.0.0.1:9090/api/health"
PASS=0
FAIL=0

ok() { echo "  PASS | $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL | $*"; FAIL=$((FAIL + 1)); }

echo "=== IG Agent Apex preflight ==="

if [[ -d "$APP" ]]; then ok "app bundle exists: $APP"; else bad "missing app bundle: $APP"; fi

PY="$APP/Contents/Resources/agent/.venv/bin/python3"
MAIN="$APP/Contents/Resources/agent/src/main.py"
if [[ -x "$PY" && -f "$MAIN" ]]; then ok "packaged python + main.py"; else bad "packaged agent incomplete"; fi

IDX="$APP/Contents/Resources/app.asar"
if [[ -f "$IDX" ]]; then ok "app.asar present"; else bad "app.asar missing"; fi

if pgrep -f "IG Agent Apex" >/dev/null 2>&1; then
  bad "stale Apex shell running — quit with Cmd+Q or: pkill -9 -f 'IG Agent Apex'"
else
  ok "no stale Apex shell"
fi

CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$HEALTH" 2>/dev/null || echo "000")
if [[ "$CODE" == "200" ]]; then
  ok ":9090 health HTTP 200 (sidecar already up — Electron will adopt)"
else
  ok ":9090 free for sidecar bind (HTTP $CODE)"
fi

if plutil -p "$APP/Contents/Info.plist" 2>/dev/null | rg -q "ElectronAsarIntegrity"; then
  bad "ElectronAsarIntegrity still set — run: bash scripts/apex-fix-asar-integrity.sh \"$APP\""
else
  ok "ElectronAsarIntegrity cleared (unsigned local build)"
fi

if lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  ok "production :8080 listener present (left untouched)"
else
  ok "production :8080 not listening"
fi

echo "---"
if (( FAIL == 0 )); then
  echo "READY — launch: open \"$APP\""
  exit 0
fi
echo "BLOCKED — fix $FAIL issue(s) above before launch"
exit 1
