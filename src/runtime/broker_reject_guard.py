"""Circuit breaker for repeated broker confirm rejections (epic/product mismatch)."""

from __future__ import annotations

import threading
import time
from typing import Any

REJECTION_SIZE = "SIZE"
REJECTION_MARGIN = "MARGIN"
REJECTION_MARKET_CLOSED = "MARKET_CLOSED"
REJECTION_RATE_LIMIT = "RATE_LIMIT"
REJECTION_BROKER_STATE = "BROKER_STATE"
REJECTION_UNKNOWN = "UNKNOWN"

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


def _should_latch(norm: str) -> bool:
    """Size, market-closed, and generic demo rejects are recoverable — do not 15-min latch."""
    if norm in (
        "MINIMUM_ORDER_SIZE_ERROR",
        "MARKET_CLOSED_WITH_EDITS",
        "MARKET_CLOSED",
        "UNKNOWN",
    ):
        return False
    try:
        from system.demo_execution_plane import demo_throughput_active

        if demo_throughput_active():
            if norm in _INSTRUMENT_MISMATCH_MARKERS:
                return True
            return False
    except Exception:
        pass
    return True


def _normalize_reason(reason: str) -> str:
    key = str(reason or "").strip().upper()
    if "MINIMUM_ORDER_SIZE" in key:
        return "MINIMUM_ORDER_SIZE_ERROR"
    for marker in _INSTRUMENT_MISMATCH_MARKERS:
        if marker in key:
            return marker
    return key or "UNKNOWN"


def classify_rejection(reason: str) -> str:
    """Classify IG rejection for GUI and unified state."""
    key = _normalize_reason(reason)
    if "MINIMUM_ORDER_SIZE" in key or "DEAL_SIZE" in key or "SIZE" in key:
        return REJECTION_SIZE
    if "MARGIN" in key or "INSUFFICIENT" in key or "FUNDS" in key:
        return REJECTION_MARGIN
    if "MARKET_CLOSED" in key or "NOT_TRADEABLE" in key or "CLOSED" in key:
        return REJECTION_MARKET_CLOSED
    if "RATE" in key or "LIMIT" in key and "ORDER" not in key:
        return REJECTION_RATE_LIMIT
    if "BROKER" in key or "STATE" in key or any(m in key for m in _INSTRUMENT_MISMATCH_MARKERS):
        return REJECTION_BROKER_STATE
    return REJECTION_UNKNOWN


def record_rejection(
    *,
    epic: str,
    reason: str,
    classification: str | None = None,
    self_correction_attempted: bool = False,
    broker_epic: str = "",
) -> dict[str, Any]:
    """Never silent — log, unified state, and circuit-breaker accounting."""
    norm = _normalize_reason(reason)
    cls = classification or classify_rejection(norm)
    trip = record_broker_confirm_rejection(
        reason=norm,
        epic=epic,
        broker_epic=broker_epic,
    )
    try:
        from system.unified_runtime_state import record_rejection as _urs_record

        _urs_record(
            epic=epic,
            reason=norm,
            classification=cls,
            self_correction_attempted=self_correction_attempted,
            extra={"broker_epic": broker_epic, "tripped": trip.get("tripped")},
        )
    except Exception:
        pass
    try:
        from system.engine_log import log_engine

        log_engine(
            f"BrokerReject: {cls} epic={epic} reason={norm} "
            f"self_correct={self_correction_attempted}"
        )
    except Exception:
        pass
    return {"reason": norm, "classification": cls, **trip}


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
        tripped = count >= _trip_threshold and _should_latch(norm)
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


_post_blocked_epics: set[str] = set()
_post_blocked_until: dict[str, float] = {}
_POST_BLOCK_SEC = 3600.0


def record_epic_post_block(epic: str, *, reason: str = "", ttl_sec: float | None = None) -> None:
    """Session block for epics that fail POST (403 exchange access, invalid instrument)."""
    key = str(epic or "").strip()
    if not key:
        return
    ttl = float(ttl_sec if ttl_sec is not None else _POST_BLOCK_SEC)
    with _lock:
        _post_blocked_epics.add(key)
        _post_blocked_until[key] = time.time() + max(60.0, ttl)
    try:
        from system.engine_log import log_engine

        log_engine(f"BrokerReject: post_blocked epic={key} reason={reason or 'post_failed'}")
    except Exception:
        pass


def epic_post_blocked(epic: str) -> bool:
    key = str(epic or "").strip()
    if not key:
        return False
    now = time.time()
    with _lock:
        until = _post_blocked_until.get(key, 0.0)
        if until and until > now:
            return True
        if key in _post_blocked_epics:
            _post_blocked_epics.discard(key)
            _post_blocked_until.pop(key, None)
    return False


def broker_reject_dispatch_blocked() -> tuple[bool, str]:
    """True when instrument mismatch latch is active."""
    now = time.time()
    with _lock:
        expired = [k for k, until in _latched_until.items() if until <= now]
        for key in expired:
            _latched_until.pop(key, None)
            _consecutive.pop(key, None)
        # Size errors are recoverable — do not block dispatch for 15 minutes.
        for size_key in (
            "MINIMUM_ORDER_SIZE_ERROR",
            "MARKET_CLOSED_WITH_EDITS",
            "MARKET_CLOSED",
            "UNKNOWN",
        ):
            _latched_until.pop(size_key, None)
            _consecutive.pop(size_key, None)
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
