#!/usr/bin/env bash
# IG Agent v30 — live-fire target factory gatekeeper.
#
# Reconciles broker ledger vs £1,000 / 60% win-rate targets.
# Milestone baseline: only trades with timestamp > 2026-06-23T09:00:00Z count
# toward +£1,000 / >60% win rate. Historical ledger rows are discarded.
# Exit 0 only when trading_ledger.json reports targets_met=true.
#
# Usage:
#   ./scripts/target_factory.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${ROOT}/src/data/logs"
FAILURE_LOG="${LOG_DIR}/target_factory_failure.log"

resolve_python() {
    local candidate
    for candidate in \
        "${ROOT}/.venv/bin/python3" \
        "${ROOT}/venv/bin/python3" \
        "$(command -v python3 2>/dev/null || true)"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    printf '%s' "python3"
}

log_msg() {
    printf '[TARGET-FACTORY] %s\n' "$*"
}

write_failure_log() {
    local exit_code="$1"
    local output="$2"
    mkdir -p "$LOG_DIR"
    {
        printf '=== TARGET FACTORY FAILURE ===\n'
        printf 'timestamp: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
        printf 'exit_code: %s\n\n' "$exit_code"
        cat "$output"
        if [ -f "${ROOT}/src/data/state/trading_ledger.json" ]; then
            printf '\n=== trading_ledger.json ===\n'
            cat "${ROOT}/src/data/state/trading_ledger.json"
        fi
    } >"$FAILURE_LOG"
}

main() {
    local py tmp_out exit_code

    py="$(resolve_python)"
    tmp_out="$(mktemp "${TMPDIR:-/tmp}/target_factory.XXXXXX")"

    log_msg "start root=${ROOT}"
    log_msg "milestone: cutoff=2026-06-23T09:00:00Z (trades array filter)"
    log_msg "audit: reconciliation read-only — no agent restart"

    (
        cd "$ROOT"
        export PYTHONPATH="${ROOT}/src"
        export TARGET_FACTORY_MILESTONE="${TARGET_FACTORY_MILESTONE_SINCE:-2026-06-23T09:00:00Z}"
        "$py" scripts/target_factory.py \
            --milestone-since "${TARGET_FACTORY_MILESTONE_SINCE:-2026-06-23T09:00:00Z}" \
            "$@"
    ) 2>&1 | tee "$tmp_out"
    exit_code="${PIPESTATUS[0]}"

    if [ "$exit_code" -ne 0 ]; then
        write_failure_log "$exit_code" "$tmp_out"
        log_msg "FAILED exit=${exit_code} — see ${FAILURE_LOG}"
        rm -f "$tmp_out"
        exit "$exit_code"
    fi

    rm -f "$tmp_out"
    log_msg "PASS — £1,000 net / 60% win rate reconciled on IG broker ledger"
}

main "$@"
