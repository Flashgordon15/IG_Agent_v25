#!/usr/bin/env bash
# RETIRED — Flight Deck v29 launch path.
# Canonical product is IG Trading Desk v31.1 (Quantum Terminal).
# This script redirects so old Dock / Spotlight / muscle-memory still work.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESK="${ROOT}/scripts/trading_desk_silent.sh"

echo "╔══════════════════════════════════════════════════════╗"
echo "║  Flight Deck launch is RETIRED                       ║"
echo "║  Redirecting → Trading Desk (Quantum Terminal :3000) ║"
echo "╚══════════════════════════════════════════════════════╝"

if [[ ! -x "${DESK}" ]]; then
  echo "ERROR: ${DESK} missing" >&2
  exit 1
fi

exec bash "${DESK}"
