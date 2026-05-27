"""
Serialize IG REST sync operations — position, transaction, and alignment paths.

Prevents overlapping GET /positions, /history/transactions, etc. from racing
and tripping IG rate limits.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.RLock()


@contextmanager
def ig_rest_sync_lock(*, timeout: float | None = None):
    """Acquire global IG REST sync lock for the duration of a REST sync block."""
    if timeout is None:
        _lock.acquire()
    elif not _lock.acquire(timeout=max(0.0, float(timeout))):
        raise TimeoutError(f"ig_rest_sync_lock unavailable after {timeout}s")
    try:
        yield
    finally:
        _lock.release()
