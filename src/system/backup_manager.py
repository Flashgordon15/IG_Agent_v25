"""Non-blocking daily compressed database backup daemon."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import project_root, resolve_path

_BACKUP_INTERVAL_SEC = float(os.environ.get("IG_BACKUP_INTERVAL_SEC", str(24 * 3600)))
_BACKUP_DIR = project_root() / "logs_archive" / "db_backups"
_daemon_thread: threading.Thread | None = None
_daemon_stop = threading.Event()
_last_backup_ts = 0.0
_lock = threading.Lock()


def backup_archive_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


def _tuning_overlay_path() -> Path:
    return resolve_path("config/tuning_overlay.json")


def _atomic_sqlite_snapshot(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        src_conn = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True, timeout=2.0)
        try:
            dst_conn = sqlite3.connect(str(tmp), timeout=5.0)
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        tmp.replace(dest)
        return True
    except Exception as exc:
        log_engine(f"backup_manager: sqlite snapshot failed {type(exc).__name__}: {exc}")
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        return False


def execute_daily_database_backup(*, force: bool = False) -> dict[str, Any]:
    """
    Freeze-read triage ledger + tuning overlay, compress to timestamped tar.gz.

    Appends compliance record for Iron Ledger on success.
    """
    global _last_backup_ts
    from system.paths import triage_db_path

    now = time.time()
    with _lock:
        if not force and _last_backup_ts > 0 and (now - _last_backup_ts) < _BACKUP_INTERVAL_SEC - 60:
            return {"ok": False, "skipped": True, "reason": "interval_not_elapsed"}

    started = time.perf_counter()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_name = f"backup_{stamp}.tar.gz"
    out_dir = backup_archive_dir()
    archive_path = out_dir / archive_name
    staging = out_dir / f".staging_{stamp}_{int(now)}"
    staging.mkdir(parents=True, exist_ok=True)

    triage_src = triage_db_path()
    triage_snap = staging / "triage.db"
    overlay_src = _tuning_overlay_path()
    overlay_snap = staging / "tuning_overlay.json"
    result: dict[str, Any] = {
        "ok": False,
        "ts": now,
        "archive": str(archive_path),
        "triage_db": str(triage_src),
        "tuning_overlay": str(overlay_src),
    }

    try:
        triage_ok = _atomic_sqlite_snapshot(triage_src, triage_snap)
        overlay_ok = False
        if overlay_src.is_file():
            shutil.copy2(overlay_src, overlay_snap)
            overlay_ok = True

        if not triage_ok and not overlay_ok:
            result["reason"] = "no_source_files"
            return result

        with tarfile.open(archive_path, "w:gz") as tar:
            if triage_snap.is_file():
                tar.add(triage_snap, arcname="triage.db")
            if overlay_snap.is_file():
                tar.add(overlay_snap, arcname="config/tuning_overlay.json")

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        size_bytes = archive_path.stat().st_size if archive_path.is_file() else 0
        result.update(
            {
                "ok": True,
                "archive_name": archive_name,
                "size_bytes": size_bytes,
                "elapsed_ms": round(elapsed_ms, 2),
                "triage_included": triage_ok,
                "overlay_included": overlay_ok,
            }
        )
        with _lock:
            _last_backup_ts = now

        try:
            from system.chaos_guardian import record_database_backup_compliance

            record_database_backup_compliance(result)
        except Exception:
            pass
        log_engine(
            f"backup_manager: archive {archive_name} ({size_bytes} bytes, {elapsed_ms:.0f}ms)"
        )
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _backup_loop() -> None:
    while not _daemon_stop.is_set():
        try:
            execute_daily_database_backup()
        except Exception as exc:
            log_engine(f"backup_manager: loop error {type(exc).__name__}: {exc}")
        _daemon_stop.wait(_BACKUP_INTERVAL_SEC)


def start_backup_daemon() -> None:
    global _daemon_thread
    if _daemon_thread is not None and _daemon_thread.is_alive():
        return
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return
    _daemon_stop.clear()
    _daemon_thread = threading.Thread(target=_backup_loop, name="db-backup-daemon", daemon=True)
    _daemon_thread.start()
    log_engine(f"backup_manager: daily daemon started (interval {_BACKUP_INTERVAL_SEC / 3600:.1f}h)")


def stop_backup_daemon() -> None:
    _daemon_stop.set()


def reset_backup_manager_for_tests() -> None:
    global _last_backup_ts, _daemon_thread
    _daemon_stop.set()
    _last_backup_ts = 0.0
    _daemon_thread = None


def get_last_backup_ts() -> float:
    with _lock:
        return float(_last_backup_ts)
