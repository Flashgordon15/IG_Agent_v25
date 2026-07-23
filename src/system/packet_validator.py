"""
Incoming quote packet validation — drop corrupt frames before hub publish.

Hot path: validate_quote_packet_fast() — bool only, zero dict alloc, no lock on accept.
Rolling 60s malformed-rate monitor triggers 5-minute feed circuit breaker.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from typing import Any

# Reject reason codes — string materialized only on reject path
REASON_OK = 0
REASON_MISSING_EPIC = 1
REASON_NON_FINITE = 2
REASON_NON_POSITIVE = 3
REASON_INVERTED_SPREAD = 4
REASON_SPREAD_TOO_WIDE = 5
REASON_OUT_OF_ORDER = 6
REASON_MALFORMED_JSON = 7
REASON_CIRCUIT_BREAKER = 8
REASON_IMPLAUSIBLE_LEVEL = 9

_REASON_TEXT = (
    "ok",
    "missing_epic",
    "non_finite_price",
    "non_positive_price",
    "inverted_spread",
    "spread_too_wide",
    "out_of_order",
    "malformed_json",
    "feed_circuit_breaker",
    "implausible_level",
)

_MALFORMED_RATE_THRESHOLD = 0.05
_ROLLING_WINDOW_SEC = 60.0
_CIRCUIT_BREAKER_DURATION_SEC = 300.0
_MAX_MID_JUMP_RATIO = 0.15
# Re-anchor after this many consecutive out-of-order rejects for one epic.
# Without this the anchor never moves once poisoned (e.g. a stale hardcoded
# seed price), so every real quote is rejected forever and the feed circuit
# breaker re-trips in a permanent 5-minute cycle (observed: fresh=0 for hours).
_OUT_OF_ORDER_REANCHOR_AFTER = 3


class _ValidatorStats:
    __slots__ = ("accepted", "rejected", "last_reject_code", "last_reject_ts")

    def __init__(self) -> None:
        self.accepted = 0
        self.rejected = 0
        self.last_reject_code = REASON_OK
        self.last_reject_ts = 0.0


_stats = _ValidatorStats()
_lock = threading.Lock()
_traffic_window: deque[tuple[float, bool]] = deque(maxlen=10_000)
_last_mid_by_epic: dict[str, float] = {}
_oo_reject_streak: dict[str, int] = {}
_cb_until: float = 0.0
_cb_triggered_at: float = 0.0


def feed_circuit_breaker_active() -> bool:
    return time.time() < _cb_until


def _record_traffic(*, accepted: bool) -> None:
    global _cb_until, _cb_triggered_at
    now = time.time()
    with _lock:
        _traffic_window.append((now, accepted))
        cutoff = now - _ROLLING_WINDOW_SEC
        while _traffic_window and _traffic_window[0][0] < cutoff:
            _traffic_window.popleft()
        total = len(_traffic_window)
        if total < 20:
            return
        bad = sum(1 for _, ok in _traffic_window if not ok)
        rate = bad / total
        if rate > _MALFORMED_RATE_THRESHOLD and not feed_circuit_breaker_active():
            _cb_until = now + _CIRCUIT_BREAKER_DURATION_SEC
            _cb_triggered_at = now
            try:
                from system.engine_log import log_engine

                log_engine(
                    f"PacketValidator: feed circuit breaker ON "
                    f"malformed_rate={rate:.1%} window={_ROLLING_WINDOW_SEC:.0f}s "
                    f"duration={_CIRCUIT_BREAKER_DURATION_SEC:.0f}s"
                )
            except Exception:
                pass


def _check_out_of_order(epic: str, bid: float, offer: float) -> int:
    mid = (bid + offer) * 0.5
    if mid <= 0:
        return REASON_OK
    prev = _last_mid_by_epic.get(epic)
    if prev and prev > 0:
        jump = abs(mid - prev) / prev
        if jump > _MAX_MID_JUMP_RATIO:
            streak = _oo_reject_streak.get(epic, 0) + 1
            if streak < _OUT_OF_ORDER_REANCHOR_AFTER:
                _oo_reject_streak[epic] = streak
                return REASON_OUT_OF_ORDER
            # Consistent new level — the anchor was wrong (stale seed or gap),
            # not the incoming feed. Accept and re-anchor.
            _oo_reject_streak[epic] = 0
            _last_mid_by_epic[epic] = mid
            try:
                from system.engine_log import log_engine

                log_engine(
                    f"PacketValidator: re-anchored {epic} mid {prev:.4f} -> {mid:.4f} "
                    f"after {streak} consecutive jump rejects"
                )
            except Exception:
                pass
            return REASON_OK
    _oo_reject_streak[epic] = 0
    _last_mid_by_epic[epic] = mid
    return REASON_OK


def validate_json_frame(raw: str | bytes) -> tuple[bool, str]:
    """Drop malformed JSON before downstream parsers."""
    try:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="strict")
        else:
            text = str(raw)
        json.loads(text)
        return True, _REASON_TEXT[REASON_OK]
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        with _lock:
            _stats.rejected += 1
            _stats.last_reject_code = REASON_MALFORMED_JSON
            _stats.last_reject_ts = time.time()
        _record_traffic(accepted=False)
        return False, _REASON_TEXT[REASON_MALFORMED_JSON]


def validate_quote_packet_fast(*, epic: str, bid: float, offer: float) -> int:
    """Zero-allocation validation — returns REASON_OK (0) or reject code."""
    if feed_circuit_breaker_active():
        return REASON_CIRCUIT_BREAKER
    if not epic:
        return REASON_MISSING_EPIC
    if not (math.isfinite(bid) and math.isfinite(offer)):
        return REASON_NON_FINITE
    if bid <= 0.0 or offer <= 0.0:
        return REASON_NON_POSITIVE
    if offer < bid:
        return REASON_INVERTED_SPREAD
    spread = offer - bid
    mid = (bid + offer) * 0.5
    if mid > 0.0 and spread / mid > 0.10:
        return REASON_SPREAD_TOO_WIDE
    try:
        from system.quote_sanity import plausible_mid_for_epic

        if not plausible_mid_for_epic(epic, mid):
            return REASON_IMPLAUSIBLE_LEVEL
    except Exception:
        pass
    oo = _check_out_of_order(epic, bid, offer)
    if oo != REASON_OK:
        return oo
    _stats.accepted += 1
    _record_traffic(accepted=True)
    return REASON_OK


def validate_quote_packet(
    *,
    epic: str,
    bid: float,
    offer: float,
    source: str = "",
) -> tuple[bool, str]:
    """Compat wrapper — prefer validate_quote_packet_fast on tick hot path."""
    _ = source
    code = validate_quote_packet_fast(epic=epic.strip() if epic else "", bid=bid, offer=offer)
    if code != REASON_OK:
        reject_packet_code(code)
        return False, _REASON_TEXT[code] if 0 <= code < len(_REASON_TEXT) else "reject"
    return True, _REASON_TEXT[REASON_OK]


def reject_packet_code(code: int) -> None:
    """Record rejection by numeric code — defer string formatting."""
    with _lock:
        _stats.rejected += 1
        _stats.last_reject_code = int(code)
        _stats.last_reject_ts = time.time()
    # Circuit-breaker drops are a consequence of the breaker, not evidence of
    # malformed traffic — counting them kept the 60s window at 100% bad and
    # re-tripped the breaker the instant it expired, forever.
    # Implausible epic bands (e.g. Yahoo ~100 on EURUSD) are intentional
    # sanity rejects — do not inflate the malformed-rate circuit breaker.
    if int(code) not in (REASON_CIRCUIT_BREAKER, REASON_IMPLAUSIBLE_LEVEL):
        _record_traffic(accepted=False)


def reject_packet(reason: str) -> None:
    with _lock:
        _stats.rejected += 1
        _stats.last_reject_ts = time.time()
        for i, text in enumerate(_REASON_TEXT):
            if text == reason:
                _stats.last_reject_code = i
                _record_traffic(accepted=False)
                return
        _stats.last_reject_code = REASON_INVERTED_SPREAD
    _record_traffic(accepted=False)


def get_packet_validator_stats() -> dict[str, Any]:
    with _lock:
        code = _stats.last_reject_code
        return {
            "ok": True,
            "accepted": _stats.accepted,
            "rejected": _stats.rejected,
            "last_reject_reason": _REASON_TEXT[code] if 0 <= code < len(_REASON_TEXT) else "",
            "last_reject_ts": _stats.last_reject_ts,
        }


def get_packet_sanitizer_health() -> dict[str, Any]:
    with _lock:
        now = time.time()
        cutoff = now - _ROLLING_WINDOW_SEC
        window = [(t, ok) for t, ok in _traffic_window if t >= cutoff]
        total = len(window)
        bad = sum(1 for _, ok in window if not ok)
        rate = (bad / total) if total > 0 else 0.0
        cb_active = now < _cb_until
        return {
            "ok": not cb_active,
            "circuit_breaker_active": cb_active,
            "circuit_breaker_remaining_sec": round(max(0.0, _cb_until - now), 1),
            "malformed_rate_60s": round(rate, 4),
            "malformed_threshold": _MALFORMED_RATE_THRESHOLD,
            "window_samples": total,
            "accepted": _stats.accepted,
            "rejected": _stats.rejected,
            "last_reject_reason": _REASON_TEXT[_stats.last_reject_code]
            if 0 <= _stats.last_reject_code < len(_REASON_TEXT)
            else "",
            "cb_triggered_at": _cb_triggered_at,
        }


def reanchor_epic_mid(epic: str) -> None:
    """Clear jump-guard anchor for an epic — used by desk Yahoo→hub heal."""
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        _last_mid_by_epic.pop(key, None)
        _oo_reject_streak.pop(key, None)


def clear_feed_circuit_breaker_for_heal(*, reason: str = "desk_heal") -> bool:
    """Lift a stuck feed circuit breaker so Yahoo hub bridge can publish."""
    global _cb_until, _cb_triggered_at
    was_active = feed_circuit_breaker_active()
    with _lock:
        _cb_until = 0.0
        _cb_triggered_at = 0.0
        # Drain reject window so heal publishes don't immediately re-trip.
        _traffic_window.clear()
    if was_active:
        try:
            from system.engine_log import log_engine

            log_engine(f"PacketValidator: circuit breaker cleared ({reason})")
        except Exception:
            pass
    return was_active


def reset_packet_validator_for_tests() -> None:
    global _stats, _cb_until, _cb_triggered_at, _last_mid_by_epic, _traffic_window
    with _lock:
        _stats = _ValidatorStats()
        _traffic_window.clear()
        _last_mid_by_epic.clear()
        _oo_reject_streak.clear()
        _cb_until = 0.0
        _cb_triggered_at = 0.0
