#!/usr/bin/env bash
# Install double-click Desktop shortcuts for the live dashboard (no agent restart).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP="${HOME}/Desktop"
OPEN_SCRIPT="${ROOT}/scripts/open_live_dashboard.sh"
COMMAND_FILE="${DESKTOP}/Open IG Agent.command"
WEBLOC_FILE="${DESKTOP}/IG Agent Live.webloc"

chmod +x "${OPEN_SCRIPT}"

cat > "${COMMAND_FILE}" <<EOF
#!/bin/bash
cd "${ROOT}"
exec "${OPEN_SCRIPT}"
EOF
chmod +x "${COMMAND_FILE}"

cat > "${WEBLOC_FILE}" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>URL</key>
    <string>http://127.0.0.1:8080/</string>
</dict>
</plist>
EOF

xattr -cr "${COMMAND_FILE}" "${WEBLOC_FILE}" 2>/dev/null || true

echo "Installed:"
echo "  ${COMMAND_FILE}  (double-click — checks :8080 then opens browser)"
echo "  ${WEBLOC_FILE}    (Safari bookmark — use after signing in once)"
echo ""
echo "Live dashboard password: IG_PASSWORD from ${ROOT}/.env"

if command -v osascript >/dev/null 2>&1; then
  osascript -e 'display notification "Double-click Open IG Agent on your Desktop" with title "IG Agent"' 2>/dev/null || true
fi
