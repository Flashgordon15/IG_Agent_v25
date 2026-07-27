#!/bin/bash
# Create Desktop shortcut to IG Trading Desk v31.1 (native pywebview shell on :8080).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

exec "${ROOT}/scripts/install_trading_desk_app.sh"
