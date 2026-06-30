#!/bin/bash
# Build native igagent_launcher binary (optional — shell fallback always available).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${ROOT}/macos/launcher/igagent_launcher"
cd "${ROOT}/macos/supervisor"
if ! command -v go >/dev/null 2>&1; then
  echo "Go not installed — use macos/launcher/igagent_launcher.sh instead" >&2
  exit 1
fi
go build -o "${OUT}" igagent_launcher.go
chmod +x "${OUT}"
echo "Built ${OUT}"
