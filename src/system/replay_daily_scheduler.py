"""In-process daily replay trigger — 06:15 Europe/London."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from system.engine_log import log_engine
from system.replay_scheduler_runner import run_replay_pipeline

LONDON = ZoneInfo("Europe/London")
SCHEDULE_HOUR = 6
SCHEDULE_MINUTE = 15
_POLL_SEC = 30.0

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    last_fired: date | None = None
    log_engine(
        f"replay_daily_scheduler: started (fires {SCHEDULE_HOUR:02d}:"
        f"{SCHEDULE_MINUTE:02d} London, scheduled=True)"
    )
    while not _stop.is_set():
        now = datetime.now(LONDON)
        today = now.date()
        if (
            now.hour == SCHEDULE_HOUR
            and now.minute == SCHEDULE_MINUTE
            and last_fired != today
        ):
            last_fired = today
            log_engine("replay_daily_scheduler: triggering scheduled replay")
            try:
                run_replay_pipeline(scheduled=True)
            except Exception as exc:
                log_engine(
                    f"replay_daily_scheduler: error {type(exc).__name__}: {exc}"
                )
        _stop.wait(_POLL_SEC)
    log_engine("replay_daily_scheduler: stopped")


def start_replay_daily_scheduler() -> None:
    """Start daemon thread (idempotent)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        name="replay-daily-scheduler",
        daemon=True,
    )
    _thread.start()


def stop_replay_daily_scheduler() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
