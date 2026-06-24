#!/bin/bash
# Surgical core isolation — unload launchd, kill all agent PIDs, wipe IPC locks.
# Use before a clean single-instance v30 boot (singleton lock in src/main.py).
set -e

echo "=== INITIALISING SURGICAL CORE ISOLATION ==="

# 1. FORCEFULLY DEACTIVATE ALL REGISTERS IN LAUNCHD
echo "[1/4] Destroying launchd daemon jobs..."
launchctl unload ~/Library/LaunchAgents/com.igagent* 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.igagent.v30.ui" 2>/dev/null || true
rm -rf ~/Library/LaunchAgents/com.igagent*
rm -rf /Library/LaunchAgents/com.igagent*

# 2. ERADICATE EVERY SINGLE PYTHON FOOTPRINT
echo "[2/4] Killing all ghost python instances..."
killall -9 python python3 2>/dev/null || true
# macOS agents run as Python.framework/Python — plain killall python* misses them.
killall -9 Python 2>/dev/null || true
if pgrep -f "main.py" >/dev/null 2>&1; then
  pkill -9 -f "main.py" 2>/dev/null || true
fi

# 3. WIPE ALL LEGACY INTER-PROCESS FILTERS & TEMP LOCKS
echo "[3/4] Purging temporary socket and lock paths..."
rm -rf /var/tmp/ig_agent* /tmp/ig_agent* ~/.ig_agent* ~/.ig_agent_runtime.lock
rm -f src/data/.ig_agent_v29.lock src/data/.ig_agent_v30_port_8080.lock 2>/dev/null || true

# 4. ENFORCE STRICT EXCLUSIVITY TO V30 PATH
echo "[4/4] Locking system down to v30 workspace..."
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -f "src/main.py" ] && [ ! -d "terminal" ]; then
  echo "[CRITICAL FAIL] Executing outside v30-production workspace. Aborting launch."
  exit 1
fi

echo "=== ENVIRONMENT SANITIZED. ALL LEGACY TRACKS REMOVED. ==="
