"""Testbed daemon PID protection — prevents orphan-kill during VS Code simulation runs."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_PID_FILE = Path("/tmp/testbed_daemon.pid")


def zombie_protection_enabled() -> bool:
    return os.environ.get("TESTBED_ALLOW_ZOMBIE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def pid_file_path() -> Path:
    raw = os.environ.get("IG_TESTBED_DAEMON_PID_FILE", "").strip()
    return Path(raw) if raw else _DEFAULT_PID_FILE


def protected_daemon_pid() -> int | None:
    path = pid_file_path()
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
    except OSError:
        pass
    return None


def is_protected_pid(pid: int) -> bool:
    protected = protected_daemon_pid()
    return protected is not None and int(pid) == protected


def claim_daemon_pid() -> None:
    if not zombie_protection_enabled():
        return
    path = pid_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
