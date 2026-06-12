#!/usr/bin/env bash
# Export IG Agent workspace for Mac Mini migration (USB, rsync, or archive).
#
# Run from project root on the SOURCE Mac (MacBook):
#   bash scripts/mac_mini_export.sh
#   bash scripts/mac_mini_export.sh --full          # include all of data_lake (~3GB+)
#   bash scripts/mac_mini_export.sh --dest ~/Desktop/IG_Agent_migration
#
# Output:
#   IG_Agent_v25_export_YYYYMMDD_HHMMSS.tar.gz  (+ MANIFEST.txt, requirements-lock.txt)
#
# Then on Mac Mini:
#   tar -xzf IG_Agent_v25_export_*.tar.gz -C ~/Projects
#   cd ~/Projects/IG_Agent_v25 && bash scripts/setup_mac_mini.sh
#
# Or over SSH (no tarball):
#   bash scripts/mac_mini_connect_check.sh
#   bash scripts/mac_mini_sync.sh
#   ssh mac-mini 'cd ~/Projects/IG_Agent_v25 && bash scripts/setup_mac_mini.sh'

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FULL_LAKE=0
DEST="${HOME}/Desktop"
INCLUDE_GIT=0

usage() {
  cat <<'EOF'
Usage: bash scripts/mac_mini_export.sh [options]

Options:
  --full              Include entire data_lake/ (events, features, models — large)
  --essential-lake    Include data_lake/state + backups only (default)
  --dest PATH         Write export to PATH (default: ~/Desktop)
  --with-git          Include .git/ in archive (enables git pull on Mini)
  -h, --help          Show this help

Creates:
  IG_Agent_v25_export_<timestamp>.tar.gz
  IG_Agent_v25_export_<timestamp>_MANIFEST.txt
  IG_Agent_v25_export_<timestamp>_requirements-lock.txt  (if .venv exists)
EOF
}

LAKE_MODE="essential"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) FULL_LAKE=1; LAKE_MODE="full" ;;
    --essential-lake) LAKE_MODE="essential" ;;
    --dest) DEST="$2"; shift ;;
    --with-git) INCLUDE_GIT=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

STAMP="$(date '+%Y%m%d_%H%M%S')"
ARCHIVE_NAME="IG_Agent_v25_export_${STAMP}.tar.gz"
ARCHIVE_PATH="${DEST}/${ARCHIVE_NAME}"
MANIFEST_PATH="${DEST}/IG_Agent_v25_export_${STAMP}_MANIFEST.txt"
LOCK_PATH="${DEST}/IG_Agent_v25_export_${STAMP}_requirements-lock.txt"
STAGING="${DEST}/.ig_agent_export_staging_${STAMP}"

mkdir -p "${DEST}"
rm -rf "${STAGING}"
mkdir -p "${STAGING}/IG_Agent_v25"

echo ""
echo "IG Agent — Mac Mini export"
echo "=========================="
echo "Source:  ${ROOT}"
echo "Staging: ${STAGING}/IG_Agent_v25"
echo "Archive: ${ARCHIVE_PATH}"
echo "Lake:    ${LAKE_MODE}"
echo ""

# Stop agent on source to avoid SQLite WAL corruption during copy
if lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "WARN: Agent is running on :8080."
  echo "      Stop it first for a clean DB snapshot:"
  echo "        cd \"${ROOT}\" && ./clean_launch.sh   # or Stop Agent in dashboard"
  read -r -p "Continue export anyway? [y/N] " ans
  [[ "${ans,,}" == "y" ]] || exit 1
fi

echo "[1/5] Rsync project tree into staging..."
RSYNC_EXCLUDES=(
  --exclude '.venv/'
  --exclude 'venv/'
  --exclude 'node_modules/'
  --exclude 'dashboard/node_modules/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '**/.DS_Store'
  --exclude '**/*.icloud'
  --exclude 'src/data/logs/*.log'
  --exclude 'src/data/.ig_agent*.lock'
  --exclude 'src/data/watchdog*.pid'
  --exclude 'emergency_stop.lock'
)

if [[ "${INCLUDE_GIT}" -eq 0 ]]; then
  RSYNC_EXCLUDES+=(--exclude '.git/')
fi

if [[ "${FULL_LAKE}" -eq 0 ]]; then
  RSYNC_EXCLUDES+=(
    --exclude 'data_lake/events/'
    --exclude 'data_lake/features/'
    --exclude 'data_lake/models/'
    --exclude 'data_lake/shadow_v26/'
    --exclude 'data_lake/*.sqlite3'
  )
fi

rsync -a "${RSYNC_EXCLUDES[@]}" "${ROOT}/" "${STAGING}/IG_Agent_v25/"

echo "[2/5] Verifying critical files..."
MISSING=0
check_file() {
  if [[ ! -e "${STAGING}/IG_Agent_v25/$1" ]]; then
    echo "  MISSING: $1"
    MISSING=1
  else
    echo "  OK: $1"
  fi
}

check_file "src/main.py"
check_file "config/config_v25.json"
check_file "config/config_v29.json"
check_file "requirements.txt"
check_file "config/credentials/credentials.json"
check_file "src/data/learning_db.sqlite3"
check_file "dashboard/dist/index.html"

if [[ ! -f "${STAGING}/IG_Agent_v25/config/external_keys.json" ]]; then
  echo "  WARN: config/external_keys.json missing (Finnhub/AlphaVantage optional)"
fi

if [[ "${MISSING}" -ne 0 ]]; then
  echo ""
  echo "ERROR: Critical files missing — fix before migrating." >&2
  rm -rf "${STAGING}"
  exit 1
fi

echo "[3/5] Recording pip lockfile..."
if [[ -x "${ROOT}/.venv/bin/pip" ]]; then
  "${ROOT}/.venv/bin/pip" freeze > "${LOCK_PATH}"
  cp "${LOCK_PATH}" "${STAGING}/IG_Agent_v25/requirements-lock.txt"
  echo "  Wrote ${LOCK_PATH}"
else
  echo "  WARN: no .venv — Mini will install from requirements.txt only"
  cp "${ROOT}/requirements.txt" "${LOCK_PATH}"
fi

echo "[4/5] Writing manifest..."
{
  echo "IG Agent v25/v29.1 — Mac Mini export manifest"
  echo "Created: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "Source host: $(hostname)"
  echo "Source path: ${ROOT}"
  echo "Lake mode: ${LAKE_MODE}"
  echo ""
  echo "Critical runtime paths (on Mini after extract):"
  echo "  ~/Projects/IG_Agent_v25/src/data/learning_db.sqlite3"
  echo "  ~/Projects/IG_Agent_v25/src/data/state/"
  echo "  ~/Projects/IG_Agent_v25/config/credentials/credentials.json"
  echo "  ~/Projects/IG_Agent_v25/config/external_keys.json"
  echo ""
  echo "Restore on Mac Mini:"
  echo "  mkdir -p ~/Projects"
  echo "  tar -xzf ${ARCHIVE_NAME} -C ~/Projects"
  echo "  cd ~/Projects/IG_Agent_v25"
  echo "  bash scripts/setup_mac_mini.sh"
  echo "  ./scripts/install_launchd.sh"
  echo ""
  echo "Or SSH sync (no tarball):"
  echo "  bash scripts/mac_mini_sync.sh"
  echo ""
  du -sh "${STAGING}/IG_Agent_v25"/* 2>/dev/null || true
} > "${MANIFEST_PATH}"

echo "[5/5] Creating archive (this may take a few minutes)..."
tar -czf "${ARCHIVE_PATH}" -C "${STAGING}" IG_Agent_v25
rm -rf "${STAGING}"

BYTES="$(du -h "${ARCHIVE_PATH}" | awk '{print $1}')"
echo ""
echo "Export complete."
echo "  Archive:   ${ARCHIVE_PATH}  (${BYTES})"
echo "  Manifest:  ${MANIFEST_PATH}"
echo "  Pip lock:  ${LOCK_PATH}"
echo ""
echo "Transfer to Mac Mini (pick one):"
echo "  USB:  copy ${ARCHIVE_NAME} to a drive, extract on Mini"
echo "  SSH:  scp \"${ARCHIVE_PATH}\" mac-mini:~/Desktop/"
echo "  Sync: bash scripts/mac_mini_sync.sh  (live rsync, skips tarball)"
echo ""
echo "On Mac Mini after extract:"
echo "  cd ~/Projects/IG_Agent_v25 && bash scripts/setup_mac_mini.sh"
echo ""
