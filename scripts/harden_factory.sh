#!/usr/bin/env bash
# IG Agent v30 — 10-cycle harden factory harness.
#
# Validates the four core matrix anomalies via tests/test_hardening_matrix.py,
# then launches the desktop cockpit (smoke test when headless).
#
# Usage:
#   ./scripts/harden_factory.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CYCLES="${HARDEN_FACTORY_CYCLES:-10}"
LOG_DIR="${ROOT}/src/data/logs"
FAILURE_LOG="${LOG_DIR}/harden_factory_failure.log"

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
    printf '[HARDEN-FACTORY] %s\n' "$*"
}

write_failure_log() {
    local cycle="$1"
    local exit_code="$2"
    local output="$3"
    mkdir -p "$LOG_DIR"
    {
        printf '=== HARDEN FACTORY FAILURE ===\n'
        printf 'timestamp: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
        printf 'cycle: %s/%s\n' "$cycle" "$CYCLES"
        printf 'exit_code: %s\n\n' "$exit_code"
        cat "$output"
    } >"$FAILURE_LOG"
}

run_matrix_tests() {
    local py tmp_out pytest_exit

    py="$(resolve_python)"
    tmp_out="$(mktemp "${TMPDIR:-/tmp}/harden_factory.XXXXXX")"

    (
        cd "$ROOT"
        export PYTHONPATH="${ROOT}/src"
        export IG_AGENT_PYTEST=1
        "$py" -m pytest tests/test_hardening_matrix.py -q --tb=short 2>&1
    ) | tee "$tmp_out"
    pytest_exit="${PIPESTATUS[0]}"
    if [ "$pytest_exit" -ne 0 ]; then
        write_failure_log "$1" "$pytest_exit" "$tmp_out"
        rm -f "$tmp_out"
        return "$pytest_exit"
    fi
    rm -f "$tmp_out"
    return 0
}

launch_cockpit() {
    local py
    py="$(resolve_python)"
    cd "$ROOT"
    export PYTHONPATH="${ROOT}/src"

    if [ -n "${HARDEN_FACTORY_SKIP_COCKPIT:-}" ]; then
        log_msg "cockpit launch skipped (HARDEN_FACTORY_SKIP_COCKPIT set)"
        return 0
    fi

    if [ -n "${HARDEN_FACTORY_COCKPIT_SMOKE:-}" ] || [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        log_msg "launching desktop cockpit smoke test"
        exec "$py" scripts/desktop_cockpit.py --smoke-test
    fi

    log_msg "launching desktop cockpit (WebKit)"
    exec ./scripts/launch_desktop_cockpit.sh --no-preflight
}

main() {
    local cycle=1

    log_msg "start root=${ROOT} cycles=${CYCLES}"
    log_msg "audit: pytest-only — no agent restart or cache purge"

    while [ "$cycle" -le "$CYCLES" ]; do
        log_msg "=== CYCLE ${cycle}/${CYCLES} ==="
        if ! run_matrix_tests "$cycle"; then
            log_msg "cycle ${cycle} FAILED — see ${FAILURE_LOG}"
            exit 1
        fi
        log_msg "cycle ${cycle}/${CYCLES} PASSED"
        cycle=$((cycle + 1))
    done

    log_msg "ALL ${CYCLES} CYCLES PASSED — launching cockpit"
    launch_cockpit
}

main "$@"
