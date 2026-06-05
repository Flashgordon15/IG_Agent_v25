"""
Portfolio-level correlation guard — caps new entries per direction per session.

Professional trading systems cap directional concentration to prevent the portfolio
from becoming a one-way bet when all markets gap together (e.g. risk-off open).
This guard blocks new entries once MAX_NEW_PER_DIRECTION entries in the same
direction have been submitted in the current session window.

This is a soft gate checked BEFORE order submission — it does not affect positions
already open, only new entries. The counter resets when reset_session() is called
(typically on each new trading session open).
"""

from __future__ import annotations

import threading
from datetime import datetime

from system.engine_log import log_engine

_lock = threading.Lock()
_buy_count: int = 0
_sell_count: int = 0
_session_key: str = ""

MAX_NEW_PER_DIRECTION = 6  # max new entries in the same direction per session
_enabled: bool = True


def _session_date_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def reset_session(*, key: str | None = None) -> None:
    global _buy_count, _sell_count, _session_key
    with _lock:
        _session_key = key or _session_date_key()
        _buy_count = 0
        _sell_count = 0
    log_engine(f"correlation_guard: session reset key={_session_key}")


def _maybe_auto_reset() -> None:
    global _buy_count, _sell_count, _session_key
    today = _session_date_key()
    if _session_key != today:
        _session_key = today
        _buy_count = 0
        _sell_count = 0


def check_and_record(direction: str) -> tuple[bool, str]:
    """
    Return (allowed, reason).

    Records the entry if allowed. Call this just before submitting an order;
    if the order is later rejected by the broker, call undo() to release the slot.
    """
    global _buy_count, _sell_count
    if not _enabled:
        return True, ""
    with _lock:
        _maybe_auto_reset()
        d = str(direction or "").upper()
        if d == "BUY":
            if _buy_count >= MAX_NEW_PER_DIRECTION:
                return (
                    False,
                    f"correlation guard: {_buy_count} BUY entries this session "
                    f"(max {MAX_NEW_PER_DIRECTION})",
                )
            _buy_count += 1
        elif d == "SELL":
            if _sell_count >= MAX_NEW_PER_DIRECTION:
                return (
                    False,
                    f"correlation guard: {_sell_count} SELL entries this session "
                    f"(max {MAX_NEW_PER_DIRECTION})",
                )
            _sell_count += 1
        return True, ""


def undo(direction: str) -> None:
    """Release one slot when an order is rejected after check_and_record was called."""
    global _buy_count, _sell_count
    with _lock:
        d = str(direction or "").upper()
        if d == "BUY":
            _buy_count = max(0, _buy_count - 1)
        elif d == "SELL":
            _sell_count = max(0, _sell_count - 1)


def snapshot() -> dict[str, object]:
    with _lock:
        return {
            "buy": _buy_count,
            "sell": _sell_count,
            "max": MAX_NEW_PER_DIRECTION,
            "session": _session_key,
        }
