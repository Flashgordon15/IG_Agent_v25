"""
Exponential backoff reconnection — phases 1-5 network hardening.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

DEFAULT_BACKOFF_SEC = (1.0, 2.0, 4.0, 8.0, 16.0)


def reconnect_with_backoff(
    operation: Callable[[], T],
    *,
    label: str = "connection",
    backoff_sec: tuple[float, ...] = DEFAULT_BACKOFF_SEC,
    on_attempt: Callable[[int, float], None] | None = None,
) -> T:
    """
    Execute operation with exponential backoff. Raises last exception after exhaustion.
    """
    last_exc: Exception | None = None
    attempts = len(backoff_sec) + 1
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_exc = exc
            if attempt >= len(backoff_sec):
                break
            delay = float(backoff_sec[attempt])
            if on_attempt is not None:
                on_attempt(attempt + 1, delay)
            time.sleep(delay)
    raise RuntimeError(
        f"{label} failed after {attempts} attempts: {last_exc}"
    ) from last_exc


def purge_ring_buffers(hub: Any) -> dict[str, int]:
    """Explicit buffer purge for high-frequency ingestion leak control."""
    cleared = {"ticks": 0, "buffers": 0}
    try:
        if hasattr(hub, "purge_stale_ticks"):
            cleared["ticks"] = int(hub.purge_stale_ticks() or 0)
        if hasattr(hub, "clear_epic_buffers"):
            hub.clear_epic_buffers()
            cleared["buffers"] = 1
    except Exception:
        pass
    return cleared
