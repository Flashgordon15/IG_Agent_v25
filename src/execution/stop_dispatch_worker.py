"""
Non-blocking broker stop/limit dispatch — REST PUT runs off the trading tick path.
"""

from __future__ import annotations

import queue
import threading
from collections import namedtuple
from typing import Callable

StopDispatchJob = namedtuple(
    "StopDispatchJob",
    "deal_id trade_id side stop epic new_limit",
)

_handler: Callable[[StopDispatchJob], bool] | None = None
_queue: queue.Queue[str | None] = queue.Queue(maxsize=256)
_pending: dict[str, StopDispatchJob] = {}
_queued_keys: set[str] = set()
_worker: threading.Thread | None = None
_lock = threading.RLock()
_sync_mode = False


def _job_key(job: StopDispatchJob) -> str:
    return f"{job.deal_id}:{job.trade_id}"


def configure_sync_mode(enabled: bool) -> None:
    """When True, jobs execute inline (tests / deterministic E2E)."""
    global _sync_mode
    _sync_mode = bool(enabled)


def reset_stop_dispatch_worker_for_tests() -> None:
    global _handler, _worker, _sync_mode, _pending, _queued_keys
    with _lock:
        _handler = None
        _sync_mode = False
        _pending.clear()
        _queued_keys.clear()
        if _worker is not None and _worker.is_alive():
            try:
                _queue.put_nowait(None)
            except queue.Full:
                pass
        _worker = None
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break


def configure_stop_dispatch(handler: Callable[[StopDispatchJob], bool]) -> None:
    global _handler
    with _lock:
        _handler = handler
    _ensure_worker()


def enqueue_stop_dispatch(job: StopDispatchJob) -> bool:
    """Queue broker stop update. Coalesces duplicate keys to the latest stop."""
    if _sync_mode:
        handler = _handler
        if handler is None:
            return False
        return bool(handler(job))
    _ensure_worker()
    key = _job_key(job)
    with _lock:
        _pending[key] = job
        if key in _queued_keys:
            return True
        try:
            _queue.put_nowait(key)
            _queued_keys.add(key)
            return True
        except queue.Full:
            _pending.pop(key, None)
            try:
                from system.engine_log import log_engine

                log_engine(
                    f"stop dispatch queue full — epic={job.epic} deal={job.deal_id} "
                    f"stop={job.stop:.5f}"
                )
            except Exception:
                pass
            return False


def wait_pending_stops(*, timeout: float = 5.0) -> None:
    """Block until queued stop jobs finish (tests)."""
    if _sync_mode:
        return
    import time

    end = time.time() + max(0.0, float(timeout))
    while pending_stop_count() > 0 and time.time() < end:
        time.sleep(0.01)
    try:
        _queue.join()
    except Exception:
        pass


def pending_stop_count() -> int:
    with _lock:
        return len(_queued_keys)


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(
            target=_worker_loop,
            name="ig-stop-dispatch",
            daemon=True,
        )
        _worker.start()


def _worker_loop() -> None:
    while True:
        key = _queue.get()
        try:
            if key is None:
                return
            with _lock:
                job = _pending.pop(key, None)
                _queued_keys.discard(key)
            if job is None:
                continue
            handler = _handler
            if handler is not None:
                handler(job)
        except Exception:
            pass
        finally:
            _queue.task_done()
