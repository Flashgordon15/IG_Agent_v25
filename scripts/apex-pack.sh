#!/usr/bin/env bash
# Project Apex Phase 8 — final macOS distribution pack (shadow desktop, production protected).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${AGENT_DIR}"

export IG_APEX_PROTECT_PRODUCTION_PORTS=1

echo "=== Apex Monolith Phase 8 — distribution pack ==="
echo "Production :8080/:8787 protected (IG_APEX_PROTECT_PRODUCTION_PORTS=1)"

bash "${SCRIPT_DIR}/apex-shadow-purge.sh"
bash "${SCRIPT_DIR}/apex-build-lock.sh"
bash "${SCRIPT_DIR}/apex-build-power-assertion.sh"

echo "[apex-pack] building WebGL dashboard bundle"
(cd dashboard && npm run build)

if [[ ! -d ".venv" ]]; then
  echo "[apex-pack] ERROR: .venv missing — create before packaging sidecar"
  exit 1
fi

if [[ ! -f "native/apex_power/no_nap" ]]; then
  echo "[apex-pack] ERROR: IOPMAssertion binary missing after build-power"
  exit 1
fi

if [[ ! -f "dashboard/dist/index.html" ]]; then
  echo "[apex-pack] ERROR: dashboard/dist not built"
  exit 1
fi

EB="${AGENT_DIR}/node_modules/.bin/electron-builder"
if [[ ! -x "${EB}" ]]; then
  EB="npx electron-builder@25.1.8"
fi

PACK_MODE="${1:-dir}"
if [[ "${PACK_MODE}" == "dir" ]]; then
  echo "[apex-pack] electron-builder --mac dir → dist-apex/"
  ${EB} --mac dir
elif [[ "${PACK_MODE}" == "full" ]]; then
  echo "[apex-pack] electron-builder --mac dmg+zip → dist-apex/"
  ${EB} --mac
else
  echo "[apex-pack] unknown mode: ${PACK_MODE} (use 'dir' or 'full')"
  exit 1
fi

APP_BUNDLE="${AGENT_DIR}/dist-apex/mac-arm64/IG Agent Apex.app"
if [[ -d "${APP_BUNDLE}" ]]; then
  bash "${SCRIPT_DIR}/apex-fix-asar-integrity.sh" "${APP_BUNDLE}"
fi

echo "[apex-pack] complete — inspect dist-apex/"
