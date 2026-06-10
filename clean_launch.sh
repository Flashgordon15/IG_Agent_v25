#!/usr/bin/env bash
# Pristine boot: kill ghost processes, clear pause flags, run overnight readiness.
#
# Usage (from project root):
#   ./clean_launch.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo ""
echo "IG Agent — CLEAN LAUNCH"
echo "======================="
echo ""

echo "[1/3] Stopping agent, watchdog, and monitor processes..."
pkill -9 -f "src/main.py" 2>/dev/null || true
pkill -9 -f "main.py" 2>/dev/null || true
pkill -9 -f "${ROOT}/scripts/watchdog.sh" 2>/dev/null || true
pkill -9 -f "watchdog_launchd.py" 2>/dev/null || true
pkill -9 -f "monitor_agent.sh" 2>/dev/null || true
pkill -9 -f "shutdown_verify_server.py" 2>/dev/null || true

echo "[2/3] Clearing dashboard API port 8080..."
lsof -t -i:8080 | xargs kill -9 2>/dev/null || true

echo "[3/3] Resetting transient state flags..."
rm -f "${ROOT}/src/data/state/manual_stop.json"

echo ""
echo "Slate clean — no ghost processes, port 8080 free, manual_stop cleared."
echo "Handing off to overnight readiness bootstrap..."
echo ""

exec "${ROOT}/scripts/ensure_overnight_ready.sh"
