#!/usr/bin/env bash
# IG Agent v29 — weekly full-application backup with 2-slot rotation.
#
# Destination (default): ~/Backups/IG_Agent_v25/
#   IG_Agent_v25_backup_A/  — slot A (week 1 / week 3 / …)
#   IG_Agent_v25_backup_B/  — slot B (week 2 / week 4 / …)
#   .rotation_state         — last written slot ("A" or "B"); next run overwrites the other
#
# Retention: exactly two full copies. Each weekly run replaces the older slot.
#
# Includes: source, config, dashboard source, scripts, docs, src/data (SQLite/state),
#           credentials path when present (.git included for restore).
# Excludes: .venv, node_modules, dashboard/dist, __pycache__, ephemeral locks, large logs.
#
# Logs: src/data/logs/backup.log (never logs credential contents).
# Schedule: Sunday 07:00 local via com.igagent.v25backup.plist
#
# Usage:
#   ./scripts/weekly_app_backup.sh
#   ./scripts/weekly_app_backup.sh --dry-run
#   BACKUP_ROOT=~/Backups/IG_Agent_v25 ./scripts/weekly_app_backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${IG_AGENT_ROOT:-}" ]; then
  ROOT="${IG_AGENT_ROOT}"
else
  ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

BACKUP_ROOT="${BACKUP_ROOT:-${HOME}/Backups/IG_Agent_v25}"
LOG_FILE="${ROOT}/src/data/logs/backup.log"
ROTATION_STATE="${BACKUP_ROOT}/.rotation_state"
SLOT_A_NAME="IG_Agent_v25_backup_A"
SLOT_B_NAME="IG_Agent_v25_backup_B"
SLOT_A_DIR="${BACKUP_ROOT}/${SLOT_A_NAME}"
SLOT_B_DIR="${BACKUP_ROOT}/${SLOT_B_NAME}"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      cat <<EOF
Usage: $(basename "$0") [--dry-run]

Weekly full-application backup with 2-slot A/B rotation.

  --dry-run   Show planned rsync actions without writing backups

Environment:
  BACKUP_ROOT   Backup parent directory (default: ~/Backups/IG_Agent_v25)
  IG_AGENT_ROOT Project root (default: parent of scripts/)
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  mkdir -p "$(dirname "${LOG_FILE}")"
  echo "${msg}" >> "${LOG_FILE}"
  echo "${msg}"
}

pick_target_slot() {
  local last=""
  if [ -f "${ROTATION_STATE}" ]; then
    last="$(tr -d '[:space:]' < "${ROTATION_STATE}" || true)"
  fi
  case "${last}" in
    A) echo "B" ;;
    B) echo "A" ;;
    *) echo "A" ;;
  esac
}

RSYNC_EXCLUDES=(
  --exclude '.venv/'
  --exclude 'venv/'
  --exclude 'node_modules/'
  --exclude 'dashboard/node_modules/'
  --exclude 'dashboard/dist/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '**/.DS_Store'
  --exclude '**/*.icloud'
  --exclude 'src/data/logs/*.log'
  --exclude 'src/data/logs/archive/'
  --exclude 'src/data/.ig_agent*.lock'
  --exclude 'src/data/watchdog*.pid'
  --exclude 'emergency_stop.lock'
)

mkdir -p "${BACKUP_ROOT}" "$(dirname "${LOG_FILE}")"

TARGET_SLOT="$(pick_target_slot)"
if [ "${TARGET_SLOT}" = "A" ]; then
  TARGET_DIR="${SLOT_A_DIR}"
  OTHER_DIR="${SLOT_B_DIR}"
  OTHER_SLOT="B"
else
  TARGET_DIR="${SLOT_B_DIR}"
  OTHER_DIR="${SLOT_A_DIR}"
  OTHER_SLOT="A"
fi

CRED_STATUS="absent"
if [ -f "${ROOT}/config/credentials/credentials.json" ]; then
  CRED_STATUS="present"
fi

AGENT_STATUS="stopped"
if lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  AGENT_STATUS="running"
fi

log "=== weekly app backup start (dry_run=${DRY_RUN}) ==="
log "source=${ROOT}"
log "backup_root=${BACKUP_ROOT}"
log "target_slot=${TARGET_SLOT} (${TARGET_DIR})"
log "retain_slot=${OTHER_SLOT} (${OTHER_DIR})"
log "credentials=${CRED_STATUS}"
log "agent_on_8080=${AGENT_STATUS}"

if [ "${AGENT_STATUS}" = "running" ]; then
  log "WARN: agent running during backup — SQLite WAL may be mid-write (acceptable for disaster recovery)"
fi

RSYNC_FLAGS=(-a --delete "${RSYNC_EXCLUDES[@]}")
if [ "${DRY_RUN}" -eq 1 ]; then
  RSYNC_FLAGS+=(--dry-run)
fi

if [ "${DRY_RUN}" -eq 0 ]; then
  rm -rf "${TARGET_DIR}"
fi
mkdir -p "${TARGET_DIR}"

log "rsync project tree → ${TARGET_DIR}"
if ! rsync "${RSYNC_FLAGS[@]}" "${ROOT}/" "${TARGET_DIR}/"; then
  log "ERROR: rsync failed"
  exit 1
fi

if [ "${DRY_RUN}" -eq 0 ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  MANIFEST="${TARGET_DIR}/BACKUP_MANIFEST.txt"
  {
    echo "IG Agent weekly backup manifest"
    echo "created_utc=${STAMP}"
    echo "source_host=$(hostname)"
    echo "source_path=${ROOT}"
    echo "slot=${TARGET_SLOT}"
    echo "credentials=${CRED_STATUS}"
    echo "agent_on_8080=${AGENT_STATUS}"
    echo ""
    echo "Restore:"
    echo "  rsync -a ${TARGET_DIR}/ /path/to/restore/IG_Agent_v25/"
    echo "  cd /path/to/restore/IG_Agent_v25 && bash scripts/setup_mac_mini.sh"
    echo ""
    du -sh "${TARGET_DIR}" 2>/dev/null || true
  } > "${MANIFEST}"

  echo "${TARGET_SLOT}" > "${ROTATION_STATE}"
  SIZE="$(du -sh "${TARGET_DIR}" | awk '{print $1}')"
  log "backup complete slot=${TARGET_SLOT} size=${SIZE}"
  log "rotation: kept slots A+B; next run overwrites slot ${OTHER_SLOT}"
else
  log "dry-run complete — no files written"
fi

log "=== weekly app backup end ==="
exit 0
