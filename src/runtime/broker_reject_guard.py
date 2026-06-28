"""Circuit breaker for repeated broker confirm rejections (epic/product mismatch)."""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_consecutive: dict[str, int] = {}
_latched_until: dict[str, float] = {}
_latch_sec = 900.0
_trip_threshold = 3

_INSTRUMENT_MISMATCH_MARKERS = (
    "INSTRUMENT_NOT_TRADEABLE",
    "INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY",
    "UNKNOWN_EPIC",
    "INVALID_EPIC",
)


def configure_broker_reject_guard(
    *,
    trip_threshold: int | None = None,
    latch_sec: float | None = None,
) -> None:
    global _trip_threshold, _latch_sec
    if trip_threshold is not None and trip_threshold > 0:
        _trip_threshold = int(trip_threshold)
    if latch_sec is not None and latch_sec > 0:
        _latch_sec = float(latch_sec)


def reset_broker_reject_guard_for_tests() -> None:
    with _lock:
        _consecutive.clear()
        _latched_until.clear()


def _normalize_reason(reason: str) -> str:
    key = str(reason or "").strip().upper()
    for marker in _INSTRUMENT_MISMATCH_MARKERS:
        if marker in key:
            return marker
    return key or "UNKNOWN"


def record_broker_confirm_rejection(
    *,
    reason: str,
    epic: str = "",
    broker_epic: str = "",
) -> dict[str, Any]:
    """Increment mismatch counter; latch dispatch when threshold exceeded."""
    norm = _normalize_reason(reason)
    now = time.time()
    with _lock:
        count = _consecutive.get(norm, 0) + 1
        _consecutive[norm] = count
        tripped = count >= _trip_threshold
        if tripped:
            _latched_until[norm] = now + _latch_sec
            _consecutive[norm] = 0
    return {
        "reason": norm,
        "count": count,
        "tripped": tripped,
        "epic": epic,
        "broker_epic": broker_epic,
        "latched_until": _latched_until.get(norm, 0.0) if tripped else 0.0,
    }


def record_broker_confirm_success(*, reason: str = "") -> None:
    """Clear consecutive counter for reason family on successful fill."""
    norm = _normalize_reason(reason) if reason else ""
    with _lock:
        if norm:
            _consecutive.pop(norm, None)
            _latched_until.pop(norm, None)
        else:
            _consecutive.clear()
            _latched_until.clear()


def broker_reject_dispatch_blocked() -> tuple[bool, str]:
    """True when instrument mismatch latch is active."""
    now = time.time()
    with _lock:
        expired = [k for k, until in _latched_until.items() if until <= now]
        for key in expired:
            _latched_until.pop(key, None)
            _consecutive.pop(key, None)
        if not _latched_until:
            return False, ""
        key = max(_latched_until, key=_latched_until.get)
        until = _latched_until[key]
        remaining = max(0, int(until - now))
        return True, f"broker_reject_latched:{key}:{remaining}s"


def broker_reject_guard_status() -> dict[str, Any]:
    now = time.time()
    with _lock:
        active = {
            k: max(0, int(v - now))
            for k, v in _latched_until.items()
            if v > now
        }
        return {
            "consecutive": dict(_consecutive),
            "latched": active,
            "trip_threshold": _trip_threshold,
            "latch_sec": _latch_sec,
        }
