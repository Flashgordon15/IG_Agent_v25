"""
Idempotent single-instance lock — port-scoped, fail-closed on live siblings.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from system.engine_log import log_engine
from system.identity.app_identity import RuntimeIdentity

_acquired = False
_acquired_path: Path | None = None
_atexit_registered = False


def lock_path(port: int | None = None) -> Path:
    """Canonical port-scoped lock path — ignores legacy env basename overrides."""
    return RuntimeIdentity.get_lock_path(port)


def read_lock_holder(path: Path | None = None) -> int | None:
    """Return PID from lock file, or None if absent/unparseable."""
    target = path if path is not None else lock_path()
    if not target.is_file():
        return None
    try:
        raw = target.read_text(encoding="utf-8").strip()
        pid = int(raw.split()[0]) if raw else 0
        return pid if pid > 0 else None
    except (ValueError, OSError):
        return None


def pid_alive(pid: int) -> bool:
    """True when ``kill(pid, 0)`` succeeds — live sibling detection."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _clear_stale_lock_file(path: Path, my_pid: int) -> None:
    if not path.is_file():
        return
    holder = read_lock_holder(path)
    if holder is None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    if holder == my_pid:
        return
    if pid_alive(holder):
        return
    try:
        path.unlink(missing_ok=True)
        log_engine(f"instance_lock: cleared stale lock {path.name} (pid={holder} dead)")
    except OSError as exc:
        log_engine(
            f"instance_lock: could not clear stale lock {path.name}: "
            f"{type(exc).__name__}: {exc}"
        )


def _unlink_if_owned(path: Path) -> None:
    my_pid = os.getpid()
    holder = read_lock_holder(path)
    if holder is None:
        return
    if holder == my_pid or not pid_alive(holder):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def lock_held_by_current_process() -> bool:
    """True when this PID holds the canonical instance lock."""
    if not _acquired:
        return False
    return read_lock_holder(lock_path()) == os.getpid()


def acquire_instance_lock() -> tuple[bool, str]:
    """
    Idempotent lock acquire.

    - Returns ``(True, "ok")`` when this PID holds the lock (including re-entry).
    - Returns ``(False, reason)`` when a **live** sibling holds the lock.
    - Reclaims stale locks (dead PID) automatically.
    """
    global _acquired, _acquired_path, _atexit_registered

    if os.environ.get("IG_AGENT_ALLOW_MULTI_INSTANCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True, "multi-instance override"

    my_pid = os.getpid()
    target = lock_path()

    if _acquired and _acquired_path == target:
        holder = read_lock_holder(target)
        if holder == my_pid:
            return True, "ok"

    for legacy in RuntimeIdentity.legacy_lock_paths():
        if legacy.resolve() == target.resolve():
            continue
        _clear_stale_lock_file(legacy, my_pid)

    holder = read_lock_holder(target)
    if holder is not None and holder != my_pid:
        if pid_alive(holder):
            return (
                False,
                f"live sibling holds instance lock (pid={holder}, lock={target.name})",
            )
        _clear_stale_lock_file(target, my_pid)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{my_pid}\n", encoding="utf-8")
        _acquired = True
        _acquired_path = target
        if not _atexit_registered:
            atexit.register(release_instance_lock)
            _atexit_registered = True
        RuntimeIdentity.export_pointer_for_scripts()
        log_engine(
            f"Instance lock acquired pid={my_pid} port={RuntimeIdentity.resolve_api_port()} "
            f"lock={target.name}"
        )
        return True, "ok"
    except OSError as exc:
        return False, f"could not acquire instance lock: {type(exc).__name__}: {exc}"


def release_instance_lock() -> None:
    global _acquired, _acquired_path
    if not _acquired:
        return
    _unlink_if_owned(lock_path())
    _acquired = False
    _acquired_path = None


def force_release_instance_lock() -> None:
    """Shutdown path — drop lock even if acquire tracking was lost."""
    global _acquired, _acquired_path
    _unlink_if_owned(lock_path())
    for legacy in RuntimeIdentity.legacy_lock_paths():
        _unlink_if_owned(legacy)
    _acquired = False
    _acquired_path = None
