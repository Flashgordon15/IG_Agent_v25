#!/usr/bin/env bash
# Rebuild IG Apex Cockpit.app — native shortcut targets Next.js on :3000.
#
# Usage:
#   ./scripts/build_ig_apex_cockpit_app.sh
#   ./scripts/build_ig_apex_cockpit_app.sh --desktop-only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_NAME="IG Apex Cockpit.app"
REPO_BUNDLE="${ROOT}/launcher/${BUNDLE_NAME}"
DESKTOP_BUNDLE="${HOME}/Desktop/${BUNDLE_NAME}"
COCKPIT_UI_URL="${IG_COCKPIT_UI_URL:-http://localhost:3000}"

copy_icons() {
  local src="$1"
  local dst="$2"
  if [[ -d "${src}/Contents/Resources" ]]; then
    mkdir -p "${dst}/Contents/Resources"
    cp -R "${src}/Contents/Resources/." "${dst}/Contents/Resources/" 2>/dev/null || true
  elif [[ -f "${ROOT}/launcher/icon_source/icon.png" ]]; then
    mkdir -p "${dst}/Contents/Resources/AppIcon.iconset"
    # minimal — reuse v29 icon pipeline if present
    if [[ -f "${ROOT}/launcher/IG Agent v29.0.app/Contents/Resources/icon.icns" ]]; then
      mkdir -p "${dst}/Contents/Resources"
      cp "${ROOT}/launcher/IG Agent v29.0.app/Contents/Resources/icon.icns" \
        "${dst}/Contents/Resources/AppIcon.icns"
    fi
  fi
}

write_bundle() {
  local bundle="$1"
  rm -rf "${bundle}"
  mkdir -p "${bundle}/Contents/MacOS"
  mkdir -p "${bundle}/Contents/Resources"

  cat >"${bundle}/Contents/MacOS/IG_Apex_Cockpit" <<'LAUNCHER'
#!/usr/bin/env bash
# IG Apex Cockpit — Next.js Quantum Terminal (:3000). Finder-safe PATH + blocking UI boot.
set -euo pipefail

REPO_ROOT="__REPO_ROOT__"
COCKPIT_UI_URL="http://127.0.0.1:3000"
CURL="/usr/bin/curl"
LOG_FILE="${REPO_ROOT}/src/data/logs/apex_cockpit_launch.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export HOME="${HOME:-$(/usr/bin/id -un 2>/dev/null || echo "$USER")}"

mkdir -p "$(dirname "$LOG_FILE")"
log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >>"$LOG_FILE"; }

notify_fail() {
  /usr/bin/osascript -e "display alert \"IG Apex Cockpit\" message \"$1\" as warning" 2>/dev/null || true
}

log "launch start COCKPIT_UI_URL=${COCKPIT_UI_URL} PATH=${PATH}"

ui_ready() {
  local code
  code="$("$CURL" -s -o /dev/null -w '%{http_code}' "${COCKPIT_UI_URL}/" 2>/dev/null || echo 000)"
  [[ "$code" == "200" ]]
}

if ! ui_ready; then
  log "UI offline — starting background Next.js daemon"
  if [[ -x "${REPO_ROOT}/scripts/start_ui_background.sh" ]]; then
    if ! "${REPO_ROOT}/scripts/start_ui_background.sh" >>"$LOG_FILE" 2>&1; then
      log "start_ui_background FAILED"
      notify_fail "Next.js failed to start on port 3000. See apex_cockpit_launch.log"
      exit 1
    fi
  else
    notify_fail "Missing start_ui_background.sh in repo."
    exit 1
  fi
fi

ready=0
for _i in $(seq 1 90); do
  if ui_ready; then
    ready=1
    log "UI ready HTTP 200 (attempt ${_i})"
    break
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  log "UI never became ready after 90s"
  notify_fail "Quantum Terminal did not respond on port 3000 within 90 seconds."
  exit 1
fi

# Prefer native pywebview shell (guaranteed URL bar target); fallback to browser app window.
VENV_PY="${REPO_ROOT}/.venv/bin/python3"
if [[ -x "$VENV_PY" && -f "${REPO_ROOT}/scripts/desktop_cockpit.py" ]]; then
  log "opening pywebview desktop_cockpit.py"
  export PYTHONPATH="${REPO_ROOT}/src"
  export IG_COCKPIT_UI_URL="${COCKPIT_UI_URL}"
  exec "$VENV_PY" "${REPO_ROOT}/scripts/desktop_cockpit.py" --no-preflight
fi

log "opening Chrome app window"
if [[ -d "/Applications/Google Chrome.app" ]]; then
  exec /usr/bin/open -na "Google Chrome" --args --new-window "${COCKPIT_UI_URL}/"
elif [[ -d "/Applications/Safari.app" ]]; then
  exec /usr/bin/open -a Safari "${COCKPIT_UI_URL}/"
else
  exec /usr/bin/open "${COCKPIT_UI_URL}/"
fi
LAUNCHER
  # Inject repo root into launcher (heredoc is quoted — no shell expansion).
  sed -i '' "s|__REPO_ROOT__|${ROOT}|g" "${bundle}/Contents/MacOS/IG_Apex_Cockpit"
  chmod +x "${bundle}/Contents/MacOS/IG_Apex_Cockpit"

  cat >"${bundle}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>IG_Apex_Cockpit</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>com.igagent.apex.cockpit</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>IG Apex Cockpit</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.1</string>
  <key>CFBundleVersion</key>
  <string>2</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>LSUIElement</key>
  <false/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

  if [[ -d "${DESKTOP_BUNDLE}" ]]; then
    copy_icons "${DESKTOP_BUNDLE}" "${bundle}"
  elif [[ -d "${ROOT}/launcher/IG Agent v29.0.app" ]]; then
    copy_icons "${ROOT}/launcher/IG Agent v29.0.app" "${bundle}"
  fi

  xattr -cr "${bundle}" 2>/dev/null || true
  codesign --force --deep --sign - "${bundle}" 2>/dev/null || true
}

echo "=== Building ${BUNDLE_NAME} → ${COCKPIT_UI_URL} ==="
write_bundle "${REPO_BUNDLE}"
echo "Repo bundle: ${REPO_BUNDLE}"

if [[ "${1:-}" != "--repo-only" ]]; then
  write_bundle "${DESKTOP_BUNDLE}"
  echo "Desktop bundle: ${DESKTOP_BUNDLE}"
  xattr -dr com.apple.quarantine "${DESKTOP_BUNDLE}" 2>/dev/null || true
fi

echo "=== IG Apex Cockpit.app rebuild complete ==="
