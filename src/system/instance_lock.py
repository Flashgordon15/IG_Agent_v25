"""
Single-instance lock — prevent overlapping GUI processes hammering IG API.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from system.engine_log import log_engine
from system.paths import data_dir

_LOCK_PATH = data_dir() / ".ig_agent_v24.lock"
_acquired = False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_instance_lock() -> tuple[bool, str]:
    """
    Try to acquire the instance lock. Returns (ok, message).
    Stale locks (dead PID) are reclaimed automatically.
    """
    global _acquired
    if os.environ.get("IG_AGENT_ALLOW_MULTI_INSTANCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True, "multi-instance override"

    pid = os.getpid()
    if _LOCK_PATH.exists():
        try:
            raw = _LOCK_PATH.read_text(encoding="utf-8").strip()
            other = int(raw.split()[0]) if raw else 0
        except (ValueError, OSError):
            other = 0
        if other and other != pid and _pid_alive(other):
            return False, f"Another IG Agent instance is running (pid {other}). Quit it first."
        try:
            _LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        _LOCK_PATH.write_text(f"{pid}\n", encoding="utf-8")
        _acquired = True
        atexit.register(release_instance_lock)
        log_engine(f"Instance lock acquired pid={pid}")
        return True, "ok"
    except OSError as e:
        return False, f"Could not acquire instance lock: {e}"


def release_instance_lock() -> None:
    global _acquired
    if not _acquired:
        return
    try:
        if _LOCK_PATH.exists():
            raw = _LOCK_PATH.read_text(encoding="utf-8").strip()
            if raw.startswith(str(os.getpid())):
                _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    _acquired = False
