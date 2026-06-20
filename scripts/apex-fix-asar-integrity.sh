#!/usr/bin/env bash
# electron-builder embeds ElectronAsarIntegrity in Info.plist. Electron 33+ refuses to
# load app.asar when the hash mismatches OR the app is unsigned. For local unsigned
# builds we remove the integrity block; for signed release builds we refresh the hash.
set -euo pipefail

APP="${1:-}"
if [[ -z "$APP" || ! -d "$APP" ]]; then
  echo "usage: $0 <path/to/IG Agent Apex.app>" >&2
  exit 1
fi

ASAR="$APP/Contents/Resources/app.asar"
PLIST="$APP/Contents/Info.plist"
if [[ ! -f "$ASAR" || ! -f "$PLIST" ]]; then
  echo "missing asar or Info.plist in $APP" >&2
  exit 1
fi

if ! /usr/libexec/PlistBuddy -c "Print :ElectronAsarIntegrity" "$PLIST" >/dev/null 2>&1; then
  echo "apex-fix-asar-integrity: no integrity block (ok)"
  exit 0
fi

HAS_DEV_ID=$(security find-identity -v -p codesigning 2>/dev/null | rg -c "Developer ID Application" || true)
if [[ "${HAS_DEV_ID:-0}" -eq 0 ]]; then
  /usr/libexec/PlistBuddy -c "Delete :ElectronAsarIntegrity" "$PLIST"
  echo "apex-fix-asar-integrity: removed ElectronAsarIntegrity (unsigned local build)"
  exit 0
fi

HASH=$(shasum -a 256 "$ASAR" | awk '{print $1}')
/usr/libexec/PlistBuddy -c "Set :ElectronAsarIntegrity:Resources/app.asar:hash ${HASH}" "$PLIST"
echo "apex-fix-asar-integrity: updated signed-release hash ${HASH}"
