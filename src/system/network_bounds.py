"""Network timeout bounds — prevent hot-path stalls on dead sockets."""

from __future__ import annotations

MAX_LOOP_SAFE_TIMEOUT_SEC = 5.0


def clamp_loop_safe_timeout(timeout: float | None) -> float:
    """Cap blocking network waits that must not freeze the asyncio/event loop."""
    if timeout is None:
        return MAX_LOOP_SAFE_TIMEOUT_SEC
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return MAX_LOOP_SAFE_TIMEOUT_SEC
    if value <= 0:
        return MAX_LOOP_SAFE_TIMEOUT_SEC
    return min(value, MAX_LOOP_SAFE_TIMEOUT_SEC)


def clamp_read_timeout(method: str, timeout: float | None, *, default: float) -> float:
    """Read-only REST/HTTP calls use a strict 5s ceiling."""
    base = clamp_loop_safe_timeout(timeout if timeout is not None else default)
    if str(method or "").upper() in ("GET", "HEAD", "OPTIONS"):
        return min(base, MAX_LOOP_SAFE_TIMEOUT_SEC)
    return float(timeout if timeout is not None else default)
