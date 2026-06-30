#!/bin/bash
# Compile IGAgentSupervisor.swift → macos/launcher/IGAgentSupervisor
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${ROOT}/macos/launcher/IGAgentSupervisor.swift"
OUT="${ROOT}/macos/launcher/IGAgentSupervisor"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "ERROR: swiftc not found (install Xcode Command Line Tools)" >&2
  exit 1
fi

swiftc -O \
  -o "${OUT}" \
  "${SRC}" \
  -framework Foundation \
  -framework UserNotifications \
  -framework AppKit

chmod +x "${OUT}"
echo "✅ Built ${OUT}"
