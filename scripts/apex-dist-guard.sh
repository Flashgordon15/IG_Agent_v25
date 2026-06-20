#!/usr/bin/env bash
# Phase 4 — fail release if browser WebSocket constructors leaked into dashboard dist.
set -euo pipefail

DIST="${1:-dashboard/dist}"
if [[ ! -d "${DIST}" ]]; then
  echo "apex-dist-guard: missing ${DIST}"
  exit 1
fi

if rg -n 'WebSocket|new WebSocket' "${DIST}" 2>/dev/null; then
  echo "apex-dist-guard: FAIL — WebSocket reference found in compiled bundle"
  exit 1
fi

echo "apex-dist-guard: PASS — zero WebSocket references in ${DIST}"
