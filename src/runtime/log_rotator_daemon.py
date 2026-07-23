"""Background log rotator — PERF/idle thread, never on the 0.0ms tick lane.

Periodically size-rotates append-only logs under ``data_dir()/logs`` and
optional project log roots. All file handles are closed before return so
repeated cycles do not leak FDs.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_DEFAULT_INTERVAL_SEC = float(os.environ.get("IG_LOG_ROTATOR_INTERVAL_SEC", "300"))
_MAX_BYTES = int(os.environ.get("IG_LOG_ROTATOR_MAX_BYTES", str(20 * 1024 * 1024)))
_BACKUP_COUNT = int(os.environ.get("IG_LOG_ROTATOR_BACKUPS", "5"))

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_started = False
_last_cycle: dict[str, Any] = {
    "ts": 0.0,
    "rotated": 0,
    "scanned": 0,
    "ok": True,
}


def reset_log_rotator_for_tests() -> None:
    global _started, _thread, _last_cycle
    stop_log_rotator_daemon()
    with _lock:
        _started = False
        _thread = None
        _last_cycle = {"ts": 0.0, "rotated": 0, "scanned": 0, "ok": True}


def last_rotation_cycle() -> dict[str, Any]:
    with _lock:
        return dict(_last_cycle)


def _candidate_log_dirs(extra_dirs: list[Path] | None = None) -> list[Path]:
    dirs: list[Path] = []
    try:
        from system.paths import logs_dir

        dirs.append(logs_dir())
    except Exception:
        pass
    try:
        from system.paths import data_dir

        dirs.append(data_dir() / "logs")
    except Exception:
        pass
    if extra_dirs:
        dirs.extend(extra_dirs)
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def rotate_log_file(
    path: Path,
    *,
    max_bytes: int = _MAX_BYTES,
    backup_count: int = _BACKUP_COUNT,
) -> bool:
    """
    Size-rotate a single log file. Opens no long-lived handles — only
    ``stat`` / rename / touch, all released before return.
    """
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < int(max_bytes):
        return False

    # Prefer shared rotator (same semantics); fall back to local rename chain.
    try:
        from system.log_rotator import rotate_if_needed

        rotate_if_needed(path, max_bytes=int(max_bytes), backup_count=int(backup_count))
        return True
    except Exception:
        pass

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for i in range(int(backup_count) - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            dst = path.with_name(f"{path.name}.{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        if path.exists():
            path.rename(path.with_name(f"{path.name}.1"))
            # Explicit open/close to prove FD hygiene for tests
            with open(path, "a", encoding="utf-8"):
                pass
        return True
    except OSError:
        return False


def run_log_rotation_cycle(
    *,
    log_dirs: list[Path] | None = None,
    max_bytes: int = _MAX_BYTES,
    backup_count: int = _BACKUP_COUNT,
) -> dict[str, Any]:
    """One rotation pass — safe to call from tests with tempfile dirs."""
    scanned = 0
    rotated = 0
    errors: list[str] = []
    for d in _candidate_log_dirs(log_dirs):
        try:
            if not d.is_dir():
                continue
            for log_path in sorted(d.glob("*.log")):
                scanned += 1
                try:
                    if rotate_log_file(
                        log_path, max_bytes=max_bytes, backup_count=backup_count
                    ):
                        rotated += 1
                except Exception as exc:
                    errors.append(f"{log_path.name}:{type(exc).__name__}")
        except Exception as exc:
            errors.append(f"dir:{type(exc).__name__}")

    result = {
        "ok": not errors,
        "ts": time.time(),
        "scanned": scanned,
        "rotated": rotated,
        "errors": errors,
    }
    with _lock:
        global _last_cycle
        _last_cycle = {
            "ts": result["ts"],
            "rotated": rotated,
            "scanned": scanned,
            "ok": result["ok"],
        }
    return result


def _rotator_loop(interval_sec: float) -> None:
    while not _stop.is_set():
        try:
            run_log_rotation_cycle()
        except Exception as exc:
            log_engine(f"log_rotator_daemon: cycle error {type(exc).__name__}: {exc}")
        _stop.wait(max(5.0, float(interval_sec)))


def start_log_rotator_daemon(*, interval_sec: float | None = None) -> None:
    """Start PERF background rotator (daemon thread). Idempotent."""
    global _thread, _started
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        sec = float(interval_sec if interval_sec is not None else _DEFAULT_INTERVAL_SEC)
        _thread = threading.Thread(
            target=_rotator_loop,
            args=(sec,),
            name="perf-log-rotator",
            daemon=True,
        )
        _thread.start()
        _started = True
    log_engine(f"log_rotator_daemon: started (interval {sec:.0f}s, PERF thread)")


def stop_log_rotator_daemon() -> None:
    global _started
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    with _lock:
        _started = False
