"""
Non-blocking critical alert dispatch — Telegram HTTP off the trading tick path.

Gate evaluation and other hot paths enqueue alert payloads; a single daemon
worker drains the queue and calls send_critical_alert().
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from system.engine_log import log_engine

_QUEUE_MAX = 256
_SENTINEL: object = object()


@dataclass(frozen=True)
class CriticalAlertJob:
    message: str
    dedupe_key: str | None = None


_queue: queue.Queue[CriticalAlertJob | object] = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_stop = threading.Event()


def _worker_loop() -> None:
    while True:
        try:
            job = _queue.get(timeout=0.5)
        except queue.Empty:
            if _stop.is_set() and _queue.empty():
                break
            continue
        if job is _SENTINEL:
            _queue.task_done()
            break
        if not isinstance(job, CriticalAlertJob):
            _queue.task_done()
            continue
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(job.message, dedupe_key=job.dedupe_key)
        except Exception as e:
            log_engine(
                f"alert_dispatcher worker failed: {type(e).__name__}: {e}"
            )
        finally:
            _queue.task_done()


def start_alert_dispatcher() -> None:
    """Start the singleton daemon worker (idempotent)."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(
            target=_worker_loop,
            name="AlertDispatcherWorker",
            daemon=True,
        )
        _worker.start()


def stop_alert_dispatcher(*, drain: bool = False, timeout: float = 5.0) -> None:
    """Stop the worker; optionally drain pending alerts first."""
    global _worker
    if drain:
        flush_alert_dispatcher(timeout=timeout)
    _stop.set()
    try:
        _queue.put_nowait(_SENTINEL)
    except queue.Full:
        pass
    with _worker_lock:
        thread = _worker
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0.1, float(timeout)))
    with _worker_lock:
        _worker = None


def enqueue_critical_alert(message: str, *, dedupe_key: str | None = None) -> bool:
    """
    Queue a critical alert for background delivery.

    Returns True when queued, False when dropped (empty message or full queue).
    Never blocks on network I/O.
    """
    body = str(message or "").strip()
    if not body:
        return False
    start_alert_dispatcher()
    job = CriticalAlertJob(message=body, dedupe_key=dedupe_key)
    try:
        _queue.put_nowait(job)
        return True
    except queue.Full:
        log_engine(
            f"alert_dispatcher queue full — dropped alert: {body[:120]}"
        )
        return False


def flush_alert_dispatcher(*, timeout: float = 5.0) -> None:
    """Block until queued alerts are processed (tests / graceful shutdown)."""
    deadline = threading.Event()
    import time as _time

    end = _time.time() + max(0.1, float(timeout))
    while _time.time() < end:
        if _queue.unfinished_tasks == 0:
            return
        _time.sleep(0.05)


def reset_alert_dispatcher_for_tests() -> None:
    """Drain and stop the worker between tests."""
    stop_alert_dispatcher(drain=True, timeout=2.0)
    with _worker_lock:
        while True:
            try:
                _queue.get_nowait()
            except queue.Empty:
                break
    _stop.clear()
