#!/usr/bin/env bash
# Compile macOS IOPMAssertion helper for Electron desktop shell.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/native/apex_power/no_nap.c"
OUT="${ROOT}/native/apex_power/no_nap"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "apex-build-power: skipped (non-macOS)"
  exit 0
fi
cc -framework IOKit -framework CoreFoundation -o "${OUT}" "${SRC}"
chmod +x "${OUT}"
echo "apex-build-power: ${OUT}"
