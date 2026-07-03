#!/bin/bash
# Build Iron Cage Flight Deck AppIcon.icns from 1024px master PNG.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="${ROOT}/macos/assets"
MASTER="${ASSETS}/flight_deck_icon_1024.png"
ICONSET="${ASSETS}/AppIcon.iconset"
OUT="${ASSETS}/AppIcon.icns"

if [[ ! -f "${MASTER}" ]]; then
  echo "ERROR: missing ${MASTER}" >&2
  exit 1
fi

rm -rf "${ICONSET}"
mkdir -p "${ICONSET}"

sips -z 16 16 "${MASTER}" --out "${ICONSET}/icon_16x16.png" >/dev/null
sips -z 32 32 "${MASTER}" --out "${ICONSET}/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "${MASTER}" --out "${ICONSET}/icon_32x32.png" >/dev/null
sips -z 64 64 "${MASTER}" --out "${ICONSET}/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "${MASTER}" --out "${ICONSET}/icon_128x128.png" >/dev/null
sips -z 256 256 "${MASTER}" --out "${ICONSET}/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "${MASTER}" --out "${ICONSET}/icon_256x256.png" >/dev/null
sips -z 512 512 "${MASTER}" --out "${ICONSET}/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "${MASTER}" --out "${ICONSET}/icon_512x512.png" >/dev/null
sips -z 1024 1024 "${MASTER}" --out "${ICONSET}/icon_512x512@2x.png" >/dev/null

iconutil -c icns "${ICONSET}" -o "${OUT}"
echo "✅ ${OUT}"
