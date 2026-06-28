#!/bin/bash
# macOS notification + alert helpers for IG Agent launcher.
notify_launcher() {
  local title="${1:-IG Agent v31}"
  local message="${2:-}"
  [[ -z "${message}" ]] && return 0
  osascript -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\"" 2>/dev/null || true
}

alert_launcher() {
  local message="${1:-Launcher failed}"
  osascript -e "display alert \"IG Agent v31 Launcher\" message \"${message//\"/\\\"}\" as critical" 2>/dev/null || true
}
