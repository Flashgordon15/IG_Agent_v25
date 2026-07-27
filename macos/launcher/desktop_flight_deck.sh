#!/bin/bash
# Legacy Flight Deck entry — retired; all desktop launches route to Trading Desk v31.1.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
exec /bin/bash "${ROOT}/scripts/trading_desk_silent.sh"
