#!/bin/bash
# Guaranteed macOS cockpit launcher — WebKit (pywebview) + POSIX SHM.
# Tkinter cannot paint on macOS Aqua; do not use /usr/bin/python3 for the GUI.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export IG_COCKPIT_UI_URL="${IG_COCKPIT_UI_URL:-http://localhost:3000}"

# Preflight: ensure trading agent is up before opening GUI (skip with --no-preflight).
if [[ "${1:-}" != "--no-preflight" ]]; then
  if ! curl -sf --max-time 2 http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "COCKPIT PREFLIGHT: agent not on :8080 — desktop_cockpit will try start_agent_background.sh"
  fi
fi

exec .venv/bin/python3 scripts/desktop_cockpit.py "$@"
