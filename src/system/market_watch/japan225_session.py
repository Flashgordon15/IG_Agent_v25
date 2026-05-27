"""
Japan 225 operational session gate — IG CFD trading hours.

Delegates open/closed determination to the fund calendar
(``config/market_watch/funds/japan_225.json``), which encodes IG's actual
CFD schedule (Sun 23:00 – Fri 22:00 Europe/London with daily maintenance
breaks). DST/BST is handled by ``ZoneInfo``.

This gate is layered on top of the calendar gate to preserve the existing
session open/close transition handlers (LiveTradeGate reset, awaiting-
fresh-tick wait, in-flight registry clears on close) and the stale-quote-
stream pause. It does not duplicate the calendar's hours logic — it
delegates.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from system.config_loader import get_config
from system.engine_log import log_engine
from system.market_watch.calendar import (
    get_market_status,
    is_market_open,
    resolve_fund_for_epic,
)

JAPAN225_EPIC = "IX.D.NIKKEI.IFM.IP"
_JST = ZoneInfo("Asia/Tokyo")
_CLOSED_LOG_INTERVAL_SEC = 60.0
_FRESH_LS_TICK_MAX_AGE_SEC = 5.0
_QUOTE_STREAM_STALE_MSG = "Quote stream stale — trading paused"
_QUOTE_STREAM_RESTORED_MSG = "Fresh quote stream restored — trading resumed"
_last_closed_log_ts = 0.0
_last_japan225_open: bool | None = None
_awaiting_fresh_ls_tick = False
_quote_stream_was_stale: bool = False
_log_lock = threading.Lock()
_transition_lock = threading.RLock()
_quote_stream_log_lock = threading.Lock()


def is_japan225_epic(epic: str) -> bool:
    if not epic:
        return False
    if epic == JAPAN225_EPIC:
        return True
    fund = resolve_fund_for_epic(epic)
    return bool(fund and str(fund.get("fund_id") or "") == "japan_225")


def _jst_now(now_utc: datetime | None = None) -> datetime:
    if now_utc is None:
        return datetime.now(_JST)
    src = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    return src.astimezone(_JST)


def is_japan225_open(now_utc: datetime | None = None) -> bool:
    """True when IG's Japan 225 CFD market is open per the fund calendar.

    Delegates to ``calendar.is_market_open`` so this returns IG's actual
    CFD hours (Sun 23:00 – Fri 22:00 Europe/London with daily maintenance
    breaks), not the Tokyo cash session. If the calendar is disabled or
    the fund config is missing, returns True (fail-open) so the broker
    is the authoritative gate.
    """
    return is_market_open(JAPAN225_EPIC, at=now_utc)


def japan225_session_gate_enabled(epic: str) -> bool:
    try:
        if not bool(get_config().market_watch_enabled):
            return False
    except Exception:
        return True
    return is_japan225_epic(epic)


def japan225_closed_message(now_utc: datetime | None = None) -> str:
    """Return a friendly closed message sourced from the fund calendar."""
    status = get_market_status(JAPAN225_EPIC, at=now_utc)
    if status is not None and not status.open:
        return f"{status.message} — trading paused outside session"
    return "Japan 225 closed — trading paused outside session"


def japan225_awaiting_fresh_tick() -> bool:
    with _transition_lock:
        return _awaiting_fresh_ls_tick


def is_quote_stream_fresh(
    epic: str, *, max_age: float = _FRESH_LS_TICK_MAX_AGE_SEC
) -> bool:
    """True when the hub holds a recent quote (<= max_age) with valid bid/offer.

    Source-agnostic: any fresh quote in the hub means the live data path is
    working. A concurrent REST fallback overwrite (source="rest") during a
    Lightstreamer-active window must not flip the gate to "stale" while ticks
    are still flowing every second; freshness is the truth, not the label.
    """
    from system.market_data_hub import get_market_data_hub

    snap = get_market_data_hub().get_snapshot(epic)
    if snap is None:
        return False
    if snap.bid <= 0 or snap.offer <= 0:
        return False
    return snap.age_seconds() <= max_age


def _note_quote_stream_state(stale: bool) -> None:
    """Emit a single transition log when stale↔fresh changes."""
    global _quote_stream_was_stale
    with _quote_stream_log_lock:
        if stale and not _quote_stream_was_stale:
            _quote_stream_was_stale = True
            log_engine(_QUOTE_STREAM_STALE_MSG)
        elif not stale and _quote_stream_was_stale:
            _quote_stream_was_stale = False
            log_engine(_QUOTE_STREAM_RESTORED_MSG)


def japan225_strategy_paused(epic: str) -> tuple[bool, str]:
    """Return (paused, reason) for strategy loop gating."""
    if not japan225_session_gate_enabled(epic):
        return False, ""
    if not is_japan225_open():
        return True, japan225_closed_message()
    if japan225_awaiting_fresh_tick():
        jst = _jst_now()
        return (
            True,
            f"Japan225 session OPEN (JST {jst.strftime('%H:%M')}) — waiting for fresh tick",
        )
    fresh = is_quote_stream_fresh(epic)
    _note_quote_stream_state(stale=not fresh)
    if not fresh:
        return True, _QUOTE_STREAM_STALE_MSG
    return False, ""


def log_japan225_session_closed(now_utc: datetime | None = None) -> None:
    """Throttled INFO when strategy loop is paused outside JST session."""
    global _last_closed_log_ts
    now = time.time()
    with _log_lock:
        if now - _last_closed_log_ts < _CLOSED_LOG_INTERVAL_SEC:
            return
        _last_closed_log_ts = now
    log_engine(japan225_closed_message(now_utc))


def _invalidate_quote_cache(epic: str, bot: Any | None) -> None:
    from system.market_data_hub import get_market_data_hub

    get_market_data_hub().invalidate(epic)
    if bot is not None:
        lock = getattr(bot, "_quote_lock", None)
        if lock is not None:
            with lock:
                bot._latest_stream_quote = None
        else:
            bot._latest_stream_quote = None


def _handle_session_open_transition(
    epic: str,
    *,
    live_gate: Any | None,
    bot: Any | None,
    now_utc: datetime | None = None,
) -> None:
    global _awaiting_fresh_ls_tick
    jst = _jst_now(now_utc)
    with _transition_lock:
        _awaiting_fresh_ls_tick = True
    _invalidate_quote_cache(epic, bot)
    if live_gate is not None and hasattr(live_gate, "reset"):
        live_gate.reset()
    log_engine(
        f"Japan225 session OPEN (JST {jst.strftime('%H:%M')}) — waiting for fresh tick"
    )


def _handle_session_close_transition(epic: str, *, now_utc: datetime | None = None) -> None:
    global _awaiting_fresh_ls_tick
    jst = _jst_now(now_utc)
    with _transition_lock:
        _awaiting_fresh_ls_tick = False
    try:
        from execution.entry_inflight import clear_entry
        from execution.exit_inflight import clear_exit

        clear_entry(epic)
        clear_exit(epic)
    except Exception:
        pass
    log_engine(
        f"Japan225 session CLOSED (JST {jst.strftime('%H:%M')}) — trading paused"
    )


def poll_japan225_session_transitions(
    epic: str,
    *,
    live_gate: Any | None = None,
    bot: Any | None = None,
    now_utc: datetime | None = None,
) -> None:
    """Detect closed↔open transitions once per edge; keep LS/reconcile paths untouched."""
    global _last_japan225_open
    if not japan225_session_gate_enabled(epic):
        return
    open_now = is_japan225_open(now_utc)
    with _transition_lock:
        prev = _last_japan225_open
        _last_japan225_open = open_now
        if prev is None:
            return
        if prev is False and open_now:
            _handle_session_open_transition(
                epic, live_gate=live_gate, bot=bot, now_utc=now_utc
            )
        elif prev is True and not open_now:
            _handle_session_close_transition(epic, now_utc=now_utc)


def try_confirm_japan225_fresh_tick(epic: str) -> bool:
    """Clear post-open wait once a recent Lightstreamer hub tick is seen."""
    global _awaiting_fresh_ls_tick
    if not japan225_awaiting_fresh_tick():
        return True
    from system.market_data_hub import get_market_data_hub

    snap = get_market_data_hub().get_snapshot(epic)
    if (
        snap
        and snap.bid > 0
        and snap.offer > 0
        and snap.source == "lightstreamer"
        and snap.age_seconds() <= _FRESH_LS_TICK_MAX_AGE_SEC
    ):
        with _transition_lock:
            _awaiting_fresh_ls_tick = False
        log_engine("Japan225 fresh Lightstreamer tick confirmed — strategy resumed")
        return True
    return False


def reset_japan225_session_state_for_tests() -> None:
    """Reset module transition state (unit tests only)."""
    global _last_japan225_open, _awaiting_fresh_ls_tick, _last_closed_log_ts
    global _quote_stream_was_stale
    with _transition_lock:
        _last_japan225_open = None
        _awaiting_fresh_ls_tick = False
    with _log_lock:
        _last_closed_log_ts = 0.0
    with _quote_stream_log_lock:
        _quote_stream_was_stale = False
