#!/usr/bin/env bash
# IG Agent v29 — self-healing test-and-repair runner framework.
#
# Executes the full pytest suite, captures failure tracebacks for repair loops,
# and logs git workspace state to isolate regressions from uncommitted changes.
#
# Usage:
#   ./scripts/self_heal_test_runner.sh
#   ./scripts/self_heal_test_runner.sh --dry-run

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${IG_AGENT_ROOT:-}" ]; then
    ROOT="${IG_AGENT_ROOT}"
else
    ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

LOG_DIR="${ROOT}/src/data/logs"
FAILURE_LOG="${LOG_DIR}/failed_test_traceback.log"
EXPECTED_TEST_COUNT="${SELF_HEAL_EXPECTED_TESTS:-697}"

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h | --help)
            cat <<EOF
Usage: $(basename "$0") [--dry-run]

  (default)   Run full suite: PYTHONPATH=src python3 -m pytest tests/
  --dry-run   Print planned actions without running pytest or writing logs

Environment:
  SELF_HEAL_EXPECTED_TESTS   Expected pass banner count (default: 697)
  IG_AGENT_ROOT              Override project root
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

log_msg() {
    printf '[SELF-HEAL] %s\n' "$*"
}

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

git_workspace_report() {
    if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        printf 'Git workspace: not a git repository (%s)\n' "$ROOT"
        return 0
    fi

    local branch porcelain count
    branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    porcelain="$(git -C "$ROOT" status --porcelain 2>/dev/null || true)"
    count="$(printf '%s\n' "$porcelain" | sed '/^$/d' | wc -l | tr -d ' ')"

    printf 'Git branch: %s\n' "$branch"
    if [ "${count:-0}" -eq 0 ]; then
        printf 'Git workspace: clean — no uncommitted files\n'
    else
        printf 'Git workspace: WARNING — %s uncommitted path(s) on disk\n' "$count"
        printf '%s\n' "$porcelain"
    fi
}

write_failure_log() {
    local pytest_exit="$1"
    local pytest_output="$2"
    local git_report="$3"

    mkdir -p "$LOG_DIR"
    {
        printf '=== SELF-HEAL TEST FAILURE REPORT ===\n'
        printf 'timestamp: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
        printf 'project_root: %s\n' "$ROOT"
        printf 'pytest_exit_code: %s\n\n' "$pytest_exit"
        printf '=== GIT WORKSPACE GUARD ===\n'
        printf '%s\n\n' "$git_report"
        printf '=== FAILING TEST NAMES ===\n'
        grep -E '^FAILED ' "$pytest_output" 2>/dev/null || true
        grep -E '^(ERROR|FAILED) ' "$pytest_output" 2>/dev/null | grep -v '^FAILED tests/' || true
        printf '\n=== PYTEST TRACEBACK LOG ===\n'
        cat "$pytest_output"
    } >"$FAILURE_LOG"
}

run_pytest_suite() {
    local py tmp_out git_report pytest_exit collected

    py="$(resolve_python)"
    tmp_out="$(mktemp "${TMPDIR:-/tmp}/self_heal_pytest.XXXXXX")"
    git_report="$(git_workspace_report)"

    log_msg "git workspace scan:"
    while IFS= read -r line; do
        [ -n "$line" ] && log_msg "  $line"
    done <<EOF
$git_report
EOF

    if [ "$DRY_RUN" -eq 1 ]; then
        log_msg "DRY-RUN: would execute: PYTHONPATH=${ROOT}/src ${py} -m pytest tests/ --tb=long -v"
        log_msg "DRY-RUN: failure output would be written to ${FAILURE_LOG}"
        rm -f "$tmp_out"
        return 0
    fi

    log_msg "running full pytest suite (target ${EXPECTED_TEST_COUNT} tests)..."
    (
        cd "$ROOT"
        export PYTHONPATH="${ROOT}/src"
        export IG_AGENT_PYTEST=1
        "$py" -m pytest tests/ --tb=long -v --no-header 2>&1
    ) | tee "$tmp_out"
    pytest_exit="${PIPESTATUS[0]}"

    collected="$(grep -Eo '[0-9]+ passed' "$tmp_out" | tail -1 | awk '{print $1}' || true)"
    if [ -z "$collected" ]; then
        collected="$(grep -Eo 'collected [0-9]+' "$tmp_out" | tail -1 | awk '{print $2}' || true)"
    fi

    if [ "$pytest_exit" -eq 0 ]; then
        rm -f "$tmp_out"
        log_msg "ALL ${EXPECTED_TEST_COUNT} TESTS PASSED. CODEBASE IS CLEAN."
        if [ -n "$collected" ] && [ "$collected" != "$EXPECTED_TEST_COUNT" ]; then
            log_msg "note: pytest reported ${collected} passed (banner target ${EXPECTED_TEST_COUNT})"
        fi
        return 0
    fi

    write_failure_log "$pytest_exit" "$tmp_out" "$git_report"
    rm -f "$tmp_out"
    log_msg "pytest failed (exit ${pytest_exit}) — traceback written to ${FAILURE_LOG}"
    return "$pytest_exit"
}

main() {
    log_msg "start root=${ROOT} dry_run=${DRY_RUN}"
    run_pytest_suite
    exit_code=$?
    if [ "$DRY_RUN" -eq 1 ]; then
        log_msg "DRY-RUN complete — no tests executed"
        exit 0
    fi
    exit "$exit_code"
}

main "$@"
