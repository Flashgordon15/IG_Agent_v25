"""
Single-instance lock — prevent overlapping GUI processes hammering IG API.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from system.app_identity import INSTANCE_LOCK_FILE, LEGACY_LOCK_FILES
from system.engine_log import log_engine
from system.paths import data_dir

_acquired = False


def lock_path() -> Path:
    return data_dir() / INSTANCE_LOCK_FILE


def _legacy_lock_paths() -> list[Path]:
    return [data_dir() / name for name in LEGACY_LOCK_FILES]


def _clear_stale_lock_file(path: Path, pid: int) -> None:
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8").strip()
        other = int(raw.split()[0]) if raw else 0
    except (ValueError, OSError):
        other = 0
    if other and other != pid and _pid_alive(other):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


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
    lock = lock_path()
    for legacy in _legacy_lock_paths():
        _clear_stale_lock_file(legacy, pid)
    if lock.exists():
        try:
            raw = lock.read_text(encoding="utf-8").strip()
            other = int(raw.split()[0]) if raw else 0
        except (ValueError, OSError):
            other = 0
        if other and other != pid and _pid_alive(other):
            return False, f"Another IG Agent instance is running (pid {other}). Quit it first."
        _clear_stale_lock_file(lock, pid)

    try:
        lock.write_text(f"{pid}\n", encoding="utf-8")
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
    _unlink_lock_if_owned(lock_path())
    _acquired = False


def _unlink_lock_if_owned(path: Path) -> None:
    pid = os.getpid()
    try:
        if not path.exists():
            return
        raw = path.read_text(encoding="utf-8").strip()
        holder = int(raw.split()[0]) if raw else 0
        if holder == pid or not _pid_alive(holder):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def force_release_instance_lock() -> None:
    """Shutdown path — drop lock even if acquire tracking was lost."""
    global _acquired
    _unlink_lock_if_owned(lock_path())
    for legacy in _legacy_lock_paths():
        _unlink_lock_if_owned(legacy)
    _acquired = False
