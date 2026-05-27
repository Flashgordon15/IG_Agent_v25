"""Non-blocking overlap guards for background sync and reconcile tasks."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from collections.abc import Iterator

from system.engine_log import log_engine

_OVERLAP_LOG_INTERVAL_SEC = 60.0


class SyncTaskGuard:
    """Skip concurrent runs of the same task; release lock on success or exception."""

    def __init__(self, task_name: str) -> None:
        self.task_name = task_name
        self._lock = threading.Lock()
        self._running = False
        self._last_overlap_log_ts = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def try_begin(self) -> bool:
        if not self._lock.acquire(blocking=False):
            self._log_overlap()
            return False
        if self._running:
            self._lock.release()
            self._log_overlap()
            return False
        self._running = True
        return True

    def end(self) -> None:
        self._running = False
        self._lock.release()

    def _log_overlap(self) -> None:
        now = time.time()
        if now - self._last_overlap_log_ts < _OVERLAP_LOG_INTERVAL_SEC:
            return
        self._last_overlap_log_ts = now
        log_engine(f"{self.task_name} already running — skipped overlap")

    @contextmanager
    def guarded_run(self) -> Iterator[bool]:
        if not self.try_begin():
            yield False
            return
        try:
            yield True
        finally:
            self.end()


RECONCILE_TASK_GUARD = SyncTaskGuard("reconciliation")
