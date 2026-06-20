#!/usr/bin/env bash
# Ironclad electron-builder dir pack — avoids ENOTEMPTY rename collision on macOS.
# extraResources pre-creates productName.app before Electron.app rename; we merge post-pack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${AGENT_DIR}"

EB="${AGENT_DIR}/node_modules/.bin/electron-builder"
if [[ ! -x "${EB}" ]]; then
  EB="npx electron-builder@25.1.8"
fi

echo "[apex-electron-dir-pack] scrub dist-apex (pre-build)"
rm -rf "${AGENT_DIR}/dist-apex"

echo "[apex-electron-dir-pack] electron-builder --mac dir (arm64)"
set +e
${EB} --mac dir --arm64
BUILD_RC=$?
set -e

MAC_DIR="${AGENT_DIR}/dist-apex/mac-arm64"
APP_NAME="IG Agent Apex.app"
ELECTRON_APP="${MAC_DIR}/Electron.app"
PRODUCT_APP="${MAC_DIR}/${APP_NAME}"

if [[ -d "${ELECTRON_APP}" ]]; then
  echo "[apex-electron-dir-pack] merging Electron.app → ${APP_NAME}"
  if [[ -d "${PRODUCT_APP}/Contents/Resources" ]]; then
    mkdir -p "${ELECTRON_APP}/Contents/Resources"
    rsync -a "${PRODUCT_APP}/Contents/Resources/" "${ELECTRON_APP}/Contents/Resources/"
  fi
  rm -rf "${PRODUCT_APP}"
  mv "${ELECTRON_APP}" "${PRODUCT_APP}"
  BUILD_RC=0
fi

if [[ ! -d "${PRODUCT_APP}/Contents/MacOS" ]]; then
  echo "[apex-electron-dir-pack] ERROR: bundle incomplete at ${PRODUCT_APP}"
  exit "${BUILD_RC:-1}"
fi

if [[ -x "${SCRIPT_DIR}/apex-fix-asar-integrity.sh" ]]; then
  bash "${SCRIPT_DIR}/apex-fix-asar-integrity.sh" "${PRODUCT_APP}"
fi

echo "[apex-electron-dir-pack] OK — ${PRODUCT_APP}"
exit 0
