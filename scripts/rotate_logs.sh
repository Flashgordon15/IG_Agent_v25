#!/bin/bash
# ==============================================================================
# V30 AUTOMATED MIDNIGHT LOG ROTATOR
# Purpose: Prevents 5-day continuous tick data from bloating local disk storage.
# ==============================================================================

BACKUP_DIR="/Users/chrisgordon/Projects/IG_Agent_v25/data/logs_archive"
TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
mkdir -p "$BACKUP_DIR"

echo "Executing midnight log rotation sweep at $TIMESTAMP..."

# Core files to rotate safely without disrupting process streams
TARGET_LOGS=(
    "/tmp/ig_agent.live.log"
    "/tmp/ig_agent.shadow.log"
    "/tmp/ig_agent.orchestrator.log"
)

for LOG in "${TARGET_LOGS[@]}"; do
    if [ -f "$LOG" ]; then
        BASE_NAME=$(basename "$LOG" .log)
        # Copy current state to backup folder, compress with high gzip compression, then truncate original file instantly
        cp "$LOG" "$BACKUP_DIR/${BASE_NAME}_${TIMESTAMP}.log"
        gzip -9 "$BACKUP_DIR/${BASE_NAME}_${TIMESTAMP}.log"
        # Descriptor-preserving truncation — never redirect /dev/null (breaks open FDs on macOS).
        if command -v truncate >/dev/null 2>&1; then
            truncate -s 0 "$LOG"
        else
            LOG_PATH="$LOG" python3 - <<'PY'
import os
path = os.environ["LOG_PATH"]
with open(path, "r+b", buffering=0) as fh:
    fh.truncate(0)
PY
        fi
        echo "Successfully rotated and compressed: $LOG"
    fi
done

# Enforce clean retention: Delete compressed archives older than 14 days
find "$BACKUP_DIR" -type f -name "*.gz" -mtime +14 -delete
echo "Log rotation sweep completed cleanly. Retention policy enforced."
