"""
Fund operational calendar — open/closed windows per market watch fund config.

Used to skip IG REST/stream activity when a market is closed (e.g. Japan 225 weekend).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from system.config_loader import get_config
from system.market_watch.loader import load_fund_for_epic, load_fund_config

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# REST activities allowed when market is closed (minimal broker upkeep + read-only history).
_CLOSED_ESSENTIAL = frozenset(
    {
        "order",
        "position_sync",
        "session_auth",
        "transaction_history",
        "verify_reconcile",
        "closed_trades_refresh",
    }
)


@dataclass(frozen=True)
class MarketStatus:
    fund_id: str
    display_name: str
    epic: str
    open: bool
    reason: str
    next_open_at: datetime | None
    timezone: str

    @property
    def message(self) -> str:
        if self.open:
            return f"{self.display_name} market open"
        if self.next_open_at:
            return f"{self.display_name} closed — opens {self._fmt_next()}"
        return f"{self.display_name} closed — {self.reason}"

    def _fmt_next(self) -> str:
        if not self.next_open_at:
            return "unknown"
        return self.next_open_at.strftime("%a %d %b %H:%M %Z")


def _enabled() -> bool:
    try:
        return bool(get_config().market_watch_enabled)
    except Exception:
        return True


def resolve_fund_for_epic(epic: str) -> dict[str, Any] | None:
    if not epic:
        return None
    fund = load_fund_for_epic(epic)
    if fund:
        return fund
    cfg = get_config()
    if cfg.epic == epic:
        fid = getattr(cfg, "market_watch_fund_id", "") or "japan_225"
        return load_fund_config(fid)
    return None


def _parse_hm(value: str) -> time:
    s = str(value or "00:00").strip()
    if s in ("24:00", "24:00:00"):
        return time(23, 59, 59, 999999)
    parts = s.split(":")
    h = int(parts[0]) if parts else 0
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(h, m)


def _day_key(dt: datetime) -> str:
    return _DAY_NAMES[dt.weekday()]


def _in_range(now_t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_t <= end
    return now_t >= start or now_t <= end


def _session_active(fund: dict[str, Any], dt: datetime) -> tuple[bool, str]:
    day = _day_key(dt)
    closed_days = {str(d).lower() for d in fund.get("closed_weekdays") or []}
    if day in closed_days:
        return False, f"weekly close ({day})"

    now_t = dt.time()
    matched = False
    for block in fund.get("weekly_sessions") or []:
        days = {str(d).lower() for d in block.get("days") or []}
        if day not in days:
            continue
        open_t = _parse_hm(str(block.get("open") or "00:00"))
        close_t = _parse_hm(str(block.get("close") or "24:00"))
        if _in_range(now_t, open_t, close_t):
            matched = True
            break

    if not matched:
        return False, f"outside session ({day} {now_t.strftime('%H:%M')})"

    for brk in fund.get("daily_breaks") or []:
        brk_days = {str(d).lower() for d in brk.get("days") or []}
        if day not in brk_days:
            continue
        b_start = _parse_hm(str(brk.get("start") or "00:00"))
        b_end = _parse_hm(str(brk.get("end") or "00:00"))
        if _in_range(now_t, b_start, b_end):
            return False, f"daily break ({brk.get('label') or 'maintenance'})"

    return True, "session open"


def _next_open(fund: dict[str, Any], dt: datetime) -> datetime | None:
    tz = ZoneInfo(str(fund.get("timezone") or "Europe/London"))
    for offset_min in range(1, 8 * 24 * 60 + 1, 15):
        probe = dt + timedelta(minutes=offset_min)
        probe = probe.astimezone(tz)
        ok, _ = _session_active(fund, probe)
        if ok:
            return probe
    return None


def get_market_status(
    epic: str,
    *,
    at: datetime | None = None,
) -> MarketStatus | None:
    if not _enabled():
        return None
    fund = resolve_fund_for_epic(epic)
    if not fund:
        return None

    tz_name = str(fund.get("timezone") or "Europe/London")
    tz = ZoneInfo(tz_name)
    now = (at or datetime.now(tz)).astimezone(tz)
    open_ok, reason = _session_active(fund, now)
    nxt = None if open_ok else _next_open(fund, now)

    return MarketStatus(
        fund_id=str(fund.get("fund_id") or ""),
        display_name=str(fund.get("display_name") or fund.get("fund_id") or epic),
        epic=str(fund.get("epic") or epic),
        open=open_ok,
        reason=reason,
        next_open_at=nxt,
        timezone=tz_name,
    )


def is_market_open(epic: str, *, at: datetime | None = None) -> bool:
    status = get_market_status(epic, at=at)
    if status is None:
        return True
    return status.open


_transition_lock = threading.Lock()
_last_market_open: bool | None = None


def detect_market_closed_to_open_transition(epic: str) -> bool:
    """
    Return True once when the configured epic transitions closed -> open.

    Used to refresh stale DEMO sessions at Japan 225 evening open without
    requiring the operator to click Start DEMO again.
    """
    global _last_market_open
    if not _enabled() or not epic:
        return False
    status = get_market_status(epic)
    if status is None:
        return False
    open_now = bool(status.open)
    with _transition_lock:
        prev = _last_market_open
        _last_market_open = open_now
        if prev is False and open_now:
            return True
    return False


def detect_market_open_to_closed_transition(epic: str) -> bool:
    """Return True once when the epic transitions open -> closed (session end)."""
    global _last_market_open
    if not _enabled() or not epic:
        return False
    status = get_market_status(epic)
    if status is None:
        return False
    open_now = bool(status.open)
    with _transition_lock:
        prev = _last_market_open
        _last_market_open = open_now
        if prev is True and not open_now:
            return True
    return False


def minutes_until_market_close(epic: str, *, at: datetime | None = None) -> float | None:
    """Minutes until the market closes from ``at`` while currently open."""
    if not _enabled() or not epic:
        return None
    if not is_market_open(epic, at=at):
        return None
    fund = resolve_fund_for_epic(epic)
    if fund is None:
        return None
    tz = ZoneInfo(str(fund.get("timezone") or "Europe/London"))
    now = at.astimezone(tz) if at is not None else datetime.now(tz=tz)
    for minutes in range(1, 24 * 60 + 1):
        probe = now + timedelta(minutes=minutes)
        if not is_market_open(epic, at=probe):
            return float(minutes)
    return None


def is_session_end_flatten_window(
    epic: str,
    *,
    lead_minutes: float = 5.0,
    at: datetime | None = None,
) -> bool:
    """True when market is open and within ``lead_minutes`` of the next close."""
    mins = minutes_until_market_close(epic, at=at)
    if mins is None:
        return False
    return mins <= max(0.0, float(lead_minutes))


_MARKET_OPEN_REST_PAUSE_SEC = 60.0
_MARKET_OPEN_TXN_HISTORY_PAUSE_SEC = 120.0
_market_open_rest_pause_until: float = 0.0
_market_open_txn_history_pause_until: float = 0.0
_market_open_stream_confirmed: bool = False
_rest_pause_lock = threading.Lock()

_BACKGROUND_REST_ACTIVITIES = frozenset(
    {
        "transaction_history",
        "preview_quote",
        "position_sync",
        "keepalive_sync",
        "account_summary",
        "closed_trades_refresh",
        "verify_reconcile",
    }
)


def begin_market_open_rest_pause() -> None:
    """Pause background IG REST for stream startup priority at market open."""
    global _market_open_rest_pause_until, _market_open_stream_confirmed
    global _market_open_txn_history_pause_until
    from system.engine_log import log_engine

    with _rest_pause_lock:
        now = time.time()
        _market_open_rest_pause_until = now + _MARKET_OPEN_REST_PAUSE_SEC
        _market_open_txn_history_pause_until = now + _MARKET_OPEN_TXN_HISTORY_PAUSE_SEC
        _market_open_stream_confirmed = False
    log_engine("Market open — pausing background REST 60s")


def transaction_history_open_pause_active() -> bool:
    """True while transaction history sync is deferred after market open."""
    global _market_open_txn_history_pause_until
    with _rest_pause_lock:
        if _market_open_txn_history_pause_until <= 0:
            return False
        if time.time() >= _market_open_txn_history_pause_until:
            _market_open_txn_history_pause_until = 0.0
            return False
        return True


def seconds_until_transaction_history_resume() -> float:
    """Seconds until transaction history sync may resume after market open."""
    with _rest_pause_lock:
        if _market_open_txn_history_pause_until <= 0:
            return 0.0
        return max(0.0, _market_open_txn_history_pause_until - time.time())


def confirm_market_open_stream_live() -> None:
    """Resume background REST after DEMO stream reports live ticks."""
    global _market_open_stream_confirmed
    with _rest_pause_lock:
        _market_open_stream_confirmed = True


def background_rest_paused(activity: str = "") -> bool:
    """
    True when non-essential REST should defer to stream poll at market open.

    Clears when stream is confirmed live or the 60s protection window expires.
    """
    global _market_open_rest_pause_until, _market_open_stream_confirmed
    act = str(activity or "").lower()
    if act and act not in _BACKGROUND_REST_ACTIVITIES:
        return False
    with _rest_pause_lock:
        if _market_open_rest_pause_until <= 0:
            return False
        if _market_open_stream_confirmed:
            _market_open_rest_pause_until = 0.0
            return False
        if time.time() >= _market_open_rest_pause_until:
            _market_open_rest_pause_until = 0.0
            return False
    return True


def allow_rest_activity(
    activity: str,
    epic: str,
    *,
    bot_running: bool = False,
    force: bool = False,
) -> bool:
    """
    Return False when IG REST should be skipped for this activity (market closed).

    force=True bypasses (orders, user verify). When bot_running, position sync still allowed.
    """
    if force or not _enabled():
        return True
    act = str(activity or "").lower()
    if act == "transaction_history" and transaction_history_open_pause_active():
        return False
    if background_rest_paused(activity):
        return False
    status = get_market_status(epic)
    if status is None or status.open:
        return True
    if act in _CLOSED_ESSENTIAL:
        return True
    if bot_running and act in ("position_sync", "keepalive_sync"):
        return True
    return False
