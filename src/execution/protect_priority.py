"""Fast vs slow protect loop de-confliction.

The position-protect hub (≈50ms) and TradingLoop slow path (≈7s) both update
trailing/breakeven stops. When the fast path recently touched a trade, the slow
path defers trailing updates to avoid REST churn and stop oscillation.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_fast_touch: dict[int, float] = {}


def mark_fast_protect_touch(trade_id: int) -> None:
    """Record monotonic timestamp when fast path updates stop/trailing."""
    with _lock:
        _fast_touch[int(trade_id)] = time.monotonic()


def slow_loop_should_skip_trailing(trade_id: int, *, window_s: float = 8.0) -> bool:
    """True when slow loop should defer trailing/BE to a recent fast-path touch."""
    with _lock:
        ts = _fast_touch.get(int(trade_id))
    if ts is None:
        return False
    return (time.monotonic() - ts) < float(window_s)


def reset_protect_priority_for_tests() -> None:
    with _lock:
        _fast_touch.clear()
