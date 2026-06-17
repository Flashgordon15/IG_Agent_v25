"""Drop-safe queue helpers for cockpit telemetry IPC."""

from __future__ import annotations

import queue
from typing import Any, TypeVar

T = TypeVar("T")


def put_drop_oldest(q: queue.Queue[T], item: T) -> None:
    """
    Non-blocking enqueue — discard oldest frame on burst/freeze.

    Never blocks the producer thread (trading loop / telemetry collector).
    """
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass
