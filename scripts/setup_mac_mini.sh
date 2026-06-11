#!/usr/bin/env bash
# One-shot Mac Mini setup after USB copy. Run from project root:
#   bash scripts/setup_mac_mini.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ""
echo "IG Agent v25 — Mac Mini setup"
echo "============================="
echo "Project: $ROOT"
echo ""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ -f "$ROOT/src/main.py" ]] || fail "src/main.py missing — USB copy incomplete (iCloud stubs?)"
[[ -f "$ROOT/config/config_v29.json" ]] || fail "config/config_v29.json missing"
[[ -f "$ROOT/config/credentials/credentials.json" ]] || fail "config/credentials/credentials.json missing — copy from MacBook"

echo "[1/6] Clearing quarantine (USB)..."
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true

echo "[2/6] Removing stale locks..."
rm -f "$ROOT/src/data/.ig_agent_v25.lock"
rm -f "$ROOT/src/data/state/manual_stop.json"
rm -f "$ROOT/emergency_stop.lock"

echo "[3/6] Creating Python virtualenv..."
PY=""
for candidate in \
  "$ROOT/.venv/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
  "/opt/homebrew/bin/python3" \
  "/usr/local/bin/python3" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    PY="$candidate"
    break
  fi
done
[[ -n "$PY" ]] || fail "python3 not found — install from https://www.python.org/downloads/macos/"

rm -rf "$ROOT/.venv"
"$PY" -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install --upgrade pip -q
pip install -r "$ROOT/requirements.txt" -q

echo "[4/6] Dashboard..."
if [[ -f "$ROOT/dashboard/dist/index.html" ]]; then
  echo "       dist/ present — skip npm build"
else
  if command -v npm >/dev/null 2>&1; then
    (cd "$ROOT/dashboard" && npm install -q && npm run build)
  else
    echo "       WARN: dashboard/dist missing and npm not installed — install Node or copy dist/ from MacBook"
  fi
fi

echo "[5/6] Making scripts executable..."
chmod +x "$ROOT/clean_launch.sh" "$ROOT/scripts/"*.sh 2>/dev/null || true

echo "[6/6] Launchd supervision (optional overnight)..."
if [[ -x "$ROOT/scripts/install_launchd.sh" ]]; then
  "$ROOT/scripts/install_launchd.sh" || echo "       WARN: install_launchd failed — agent can still run manually"
fi

echo ""
echo "Setup complete."
echo ""
echo "Start the agent:"
echo "  cd \"$ROOT\""
echo "  ./clean_launch.sh"
echo ""
echo "Dashboard: http://localhost:8080"
echo ""
