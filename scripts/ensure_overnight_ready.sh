#!/usr/bin/env bash
# Pre-bed checklist: agent must survive Cursor/terminal close overnight.
#
# Usage (from project root):
#   ./scripts/ensure_overnight_ready.sh
#
# Installs persistent supervision (recommended once):
#   ./scripts/install_launchd.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=""
for candidate in \
  "${ROOT}/.venv/bin/python3" \
  "${ROOT}/venv/bin/python3" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
    PY="${candidate}"
    break
  fi
done

if [ -z "${PY}" ]; then
  echo "ERROR: python3 not found" >&2
  exit 1
fi

echo ""
echo "IG Agent v25 — OVERNIGHT READINESS"
echo "=================================="
echo ""

SUPERVISION="$(
  PYTHONPATH=src "${PY}" -c "
from system.overnight_supervision import overnight_supervision_summary
import json
print(json.dumps(overnight_supervision_summary()))
" 2>/dev/null || echo '{}'
)"

launchd_ok="$(printf '%s' "$SUPERVISION" | "${PY}" -c "import json,sys; d=json.load(sys.stdin); print('yes' if d.get('launchd_ok') else 'no')" 2>/dev/null || echo no)"
agent_sup_ok="$(printf '%s' "$SUPERVISION" | "${PY}" -c "import json,sys; d=json.load(sys.stdin); print('yes' if d.get('agent_supervision_ok') else 'no')" 2>/dev/null || echo no)"
launchd_detail="$(printf '%s' "$SUPERVISION" | "${PY}" -c "import json,sys; print(json.load(sys.stdin).get('launchd_detail',''))" 2>/dev/null || true)"
agent_detail="$(printf '%s' "$SUPERVISION" | "${PY}" -c "import json,sys; print(json.load(sys.stdin).get('agent_supervision_detail',''))" 2>/dev/null || true)"

if [ "$launchd_ok" = "yes" ]; then
  echo "[PASS] Launchd overnight bundle — ${launchd_detail}"
else
  echo "[FAIL] Launchd overnight bundle — ${launchd_detail}"
  echo "       Run once: ./scripts/install_launchd.sh"
fi

echo ""
echo "Running safe-to-leave (quick mode)..."
echo ""

set +e
PYTHONPATH=src "${PY}" scripts/safe_to_leave.py --quick
SAFE_RC=$?
set -e

echo ""
echo "=================================="

if [ "$launchd_ok" != "yes" ]; then
  echo ""
  echo "CRITICAL — Safe to Leave requires launchd (agent must not depend on Cursor)."
  echo "Run: ./scripts/install_launchd.sh"
  echo ""
fi

if [ "$SAFE_RC" -ne 0 ]; then
  echo "NOT READY — safe-to-leave checks failed."
  exit 1
fi

if [ "$launchd_ok" != "yes" ]; then
  echo "NOT READY — install launchd supervision first."
  exit 1
fi

echo "READY FOR OVERNIGHT — agent should survive IDE/terminal close."
echo "Optional: bash scripts/overnight_watch.sh >> src/data/logs/overnight_watch.log 2>&1 &"
echo ""
exit 0
