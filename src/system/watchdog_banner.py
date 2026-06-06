"""Persistent dashboard banner after repeated startup failures."""

from __future__ import annotations

import json

from system.engine_log import log_engine
from system.paths import logs_dir

_FAIL_COUNT_PATH = logs_dir() / "watchdog_restart_failures.json"
_BANNER_PATH = logs_dir() / "watchdog_failed.txt"
_THRESHOLD = 3


def _read_fail_count() -> int:
    if not _FAIL_COUNT_PATH.is_file():
        return 0
    try:
        data = json.loads(_FAIL_COUNT_PATH.read_text(encoding="utf-8"))
        return max(0, int(data.get("count", 0)))
    except Exception:
        return 0


def record_startup_failure(reason: str) -> None:
    """Increment failure count; write banner file on 3rd failure."""
    _FAIL_COUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
    count = _read_fail_count() + 1
    _FAIL_COUNT_PATH.write_text(
        json.dumps({"count": count, "last_reason": str(reason)[:200]}),
        encoding="utf-8",
    )
    if count >= _THRESHOLD:
        _BANNER_PATH.write_text(
            f"Watchdog: {count} consecutive startup failures.\n"
            f"Last: {reason}\n"
            "Delete this file and restart the agent.\n",
            encoding="utf-8",
        )
        log_engine(
            f"WATCHDOG FAILED — wrote {_BANNER_PATH.name} after {count} failures"
        )
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(
            f"Agent restart failed (attempt #{count}): {str(reason)[:120]}"
        )
    except Exception as e:
        log_engine(f"telegram watchdog alert failed: {type(e).__name__}: {e}")


def record_startup_success() -> None:
    """
    Reset consecutive failure state after a successful startup.

    Keeps watchdog banner behavior tied to *consecutive* startup failures.
    """
    if _read_fail_count() <= 0 and not _BANNER_PATH.is_file():
        return
    _FAIL_COUNT_PATH.unlink(missing_ok=True)
    _BANNER_PATH.unlink(missing_ok=True)
    log_engine("WATCHDOG reset — startup succeeded")


def banner_active() -> bool:
    return _BANNER_PATH.is_file()


def banner_message() -> str:
    if not _BANNER_PATH.is_file():
        return ""
    try:
        return _BANNER_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "Watchdog failure — manual restart required"


def reset_failures_for_tests() -> None:
    _FAIL_COUNT_PATH.unlink(missing_ok=True)
    _BANNER_PATH.unlink(missing_ok=True)
