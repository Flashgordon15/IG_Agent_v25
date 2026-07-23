#!/usr/bin/env bash
# Shared POSIX session detach for macOS / IDE terminal launches.
#
# nohup alone only blocks SIGHUP — the child stays in the launcher's session
# and gets reaped when that terminal session is cleaned up (Cursor/IDE shells).
# perl POSIX setsid (no setsid(1) on macOS) + nohup + disown = true session leader.
#
# Usage (source in caller):
#   # shellcheck source=scripts/lib/detach_exec.sh
#   source "${SCRIPT_DIR}/lib/detach_exec.sh"
#
#   detach_exec --log "${LOG_FILE}" -- "${PY}" -u src/main.py
#   echo "detached pid=${DETACH_PID}"
#
#   detach_exec --log "${LOG}" -- env VAR=val "$PY" src/main.py
#
# Sets DETACH_PID to the background pid; returns 0 on success.

detach_exec() {
  local log_file=""
  if [[ "${1:-}" == "--log" ]]; then
    log_file="$2"
    shift 2
  fi
  if [[ "${1:-}" == "--" ]]; then
    shift
  fi
  if [[ $# -lt 1 ]]; then
    echo "detach_exec: missing command" >&2
    return 2
  fi

  local pid
  if command -v perl >/dev/null 2>&1; then
    if [[ -n "${log_file}" ]]; then
      nohup /usr/bin/env perl -e 'use POSIX qw(setsid); setsid(); exec @ARGV or die "exec: $!"' \
        -- "$@" >>"${log_file}" 2>&1 &
    else
      nohup /usr/bin/env perl -e 'use POSIX qw(setsid); setsid(); exec @ARGV or die "exec: $!"' \
        -- "$@" &
    fi
    pid=$!
  else
    if [[ -n "${log_file}" ]]; then
      nohup "$@" >>"${log_file}" 2>&1 &
    else
      nohup "$@" &
    fi
    pid=$!
  fi
  disown "${pid}" 2>/dev/null || true
  DETACH_PID="${pid}"
  return 0
}
