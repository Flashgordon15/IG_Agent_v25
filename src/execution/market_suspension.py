"""IG market suspension / offline errors — block entries for 5 minutes."""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

_SUSPENSION_CODES = (
    "error.trading.market-offline",
    "error.trading.market-closed",
    "error.trading.market-unavailable",
    "error.trading.market-suspended",
)

_BLOCK_SEC = 300.0
_lock = threading.Lock()
_blocked_until: float = 0.0
_last_reason: str = ""


def _match_suspension(message: str) -> str | None:
    low = str(message or "").lower()
    for code in _SUSPENSION_CODES:
        if code in low:
            return code
    return None


def note_ig_order_error(exc: BaseException) -> bool:
    """Return True if this was a market suspension error (timer started)."""
    global _blocked_until, _last_reason
    msg = str(exc)
    code = _match_suspension(msg)
    if code is None:
        return False
    with _lock:
        _blocked_until = time.time() + _BLOCK_SEC
        _last_reason = code
    log_engine(f"Market suspended ({code}) — blocking orders 5 min")
    return True


def is_blocked() -> bool:
    with _lock:
        if time.time() >= _blocked_until:
            return False
        return True


def remaining_seconds() -> int:
    with _lock:
        return max(0, int(_blocked_until - time.time()))


def gate_detail() -> str:
    with _lock:
        if time.time() >= _blocked_until:
            return ""
        secs = int(_blocked_until - time.time())
        return f"Market suspended ({_last_reason}) — {secs}s remaining"


def snapshot() -> dict[str, Any]:
    with _lock:
        active = time.time() < _blocked_until
        return {
            "active": active,
            "reason": _last_reason if active else "",
            "remaining_sec": max(0, int(_blocked_until - time.time())) if active else 0,
        }


def clear_for_tests() -> None:
    global _blocked_until, _last_reason
    with _lock:
        _blocked_until = 0.0
        _last_reason = ""
