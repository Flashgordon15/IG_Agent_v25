#!/usr/bin/env bash
# IG Agent v29 — automated log rotation for high-throughput multi-market diagnostics.
#
# Rotates launchd + engine + watchdog logs into gzip archives, truncates live
# files in place (preserves open file descriptors), and enforces retention.
#
# Usage:
#   ./scripts/rotate_logs.sh              # rotate files over size threshold
#   ./scripts/rotate_logs.sh --force      # rotate all non-empty targets
#   ./scripts/rotate_logs.sh --dry-run    # print actions without writing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${IG_AGENT_ROOT:-}" ]; then
    ROOT="${IG_AGENT_ROOT}"
else
    ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

LOG_DIR="${ROOT}/src/data/logs"
ARCHIVE_DIR="${LOG_DIR}/archive"

# Basic size threshold — rotate when a live log exceeds this (5 MiB).
SIZE_THRESHOLD="${LOG_ROTATE_SIZE_BYTES:-5242880}"
RETENTION_DAYS="${LOG_ROTATE_RETENTION_DAYS:-14}"
MAX_ARCHIVES="${LOG_ROTATE_MAX_ARCHIVES:-14}"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        -h | --help)
            cat <<EOF
Usage: $(basename "$0") [--dry-run] [--force]

  --dry-run   Show planned rotations and purges without modifying files
  --force     Rotate all non-empty targets regardless of size threshold

Environment:
  LOG_ROTATE_SIZE_BYTES     Size threshold in bytes (default: 5242880)
  LOG_ROTATE_RETENTION_DAYS Delete archives older than N days (default: 14)
  LOG_ROTATE_MAX_ARCHIVES   Keep at most N archives per log base (default: 14)
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

TARGETS=(
    "${LOG_DIR}/launchd_stdout.log"
    "${LOG_DIR}/launchd_stderr.log"
    "${LOG_DIR}/engine.log"
    "${LOG_DIR}/watchdog.log"
)

log_msg() {
    printf '[rotate_logs] %s\n' "$*"
}

file_size() {
    local path="$1"
    if [ ! -f "$path" ]; then
        echo 0
        return
    fi
    stat -f '%z' "$path" 2>/dev/null || stat -c '%s' "$path" 2>/dev/null || wc -c <"$path"
}

archive_basename() {
    local src="$1"
    local base
    base="$(basename "$src")"
    local stamp
    stamp="$(date '+%Y-%m-%d-%H%M%S')"
    printf '%s.%s.gz' "$base" "$stamp"
}

rotate_target() {
    local target="$1"
    local size archive_name archive_path

    if [ ! -f "$target" ]; then
        log_msg "skip missing: $target"
        return 0
    fi

    size="$(file_size "$target")"
    if [ "$size" -eq 0 ]; then
        log_msg "skip empty: $target"
        return 0
    fi

    if [ "$FORCE" -eq 0 ] && [ "$size" -lt "$SIZE_THRESHOLD" ]; then
        log_msg "skip under threshold (${size}B < ${SIZE_THRESHOLD}B): $target"
        return 0
    fi

    archive_name="$(archive_basename "$target")"
    archive_path="${ARCHIVE_DIR}/${archive_name}"

    if [ "$DRY_RUN" -eq 1 ]; then
        log_msg "DRY-RUN: gzip ${size}B from ${target} -> ${archive_path}"
        log_msg "DRY-RUN: truncate in place: ${target}"
        return 0
    fi

    mkdir -p "$ARCHIVE_DIR"
    gzip -c "$target" >"$archive_path"
    : >"$target"
    log_msg "archived ${size}B -> ${archive_path} and truncated ${target}"
}

purge_retention() {
    local base count

    if [ ! -d "$ARCHIVE_DIR" ]; then
        return 0
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        while IFS= read -r old_file; do
            [ -n "$old_file" ] || continue
            log_msg "DRY-RUN: purge age>${RETENTION_DAYS}d: ${old_file}"
        done < <(find "$ARCHIVE_DIR" -type f -name '*.gz' -mtime "+${RETENTION_DAYS}" 2>/dev/null || true)
    else
        find "$ARCHIVE_DIR" -type f -name '*.gz' -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null \
            | while IFS= read -r old_file; do
                [ -n "$old_file" ] || continue
                log_msg "purged age>${RETENTION_DAYS}d: ${old_file}"
            done
    fi

    for base in launchd_stdout.log launchd_stderr.log engine.log watchdog.log; do
        count=0
        while IFS= read -r archive_file; do
            [ -n "$archive_file" ] || continue
            count=$((count + 1))
            if [ "$count" -le "$MAX_ARCHIVES" ]; then
                continue
            fi
            if [ "$DRY_RUN" -eq 1 ]; then
                log_msg "DRY-RUN: purge excess archive: ${archive_file}"
            else
                rm -f "$archive_file"
                log_msg "purged excess archive: ${archive_file}"
            fi
        done < <(ls -1t "${ARCHIVE_DIR}/${base}".*.gz 2>/dev/null || true)
    done
}

main() {
    log_msg "start root=${ROOT} threshold=${SIZE_THRESHOLD}B force=${FORCE} dry_run=${DRY_RUN}"
    mkdir -p "$LOG_DIR" "$ARCHIVE_DIR"

    local target
    for target in "${TARGETS[@]}"; do
        rotate_target "$target"
    done

    purge_retention
    log_msg "done"
}

main "$@"
