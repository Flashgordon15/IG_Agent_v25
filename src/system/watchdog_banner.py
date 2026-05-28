"""Persistent dashboard banner after repeated startup failures."""

from __future__ import annotations

import json
from pathlib import Path

from system.engine_log import log_engine
from system.paths import logs_dir

_FAIL_COUNT_PATH = logs_dir() / "watchdog_restart_failures.json"
_BANNER_PATH = logs_dir() / "watchdog_failed.txt"
_THRESHOLD = 3


def record_startup_failure(reason: str) -> None:
    """Increment failure count; write banner file on 3rd failure."""
    _FAIL_COUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    if _FAIL_COUNT_PATH.is_file():
        try:
            data = json.loads(_FAIL_COUNT_PATH.read_text(encoding="utf-8"))
            count = int(data.get("count", 0))
        except Exception:
            count = 0
    count += 1
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
