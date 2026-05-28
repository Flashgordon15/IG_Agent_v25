"""Europe/London window for historical OHLC REST backfill (07:00–22:30)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
FETCH_WINDOW_START_MINUTES = 7 * 60  # 07:00
FETCH_WINDOW_END_MINUTES = 22 * 60 + 30  # 22:30 (exclusive end)


def _london_minutes(now: datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=LONDON)
    else:
        now = now.astimezone(LONDON)
    return now.hour * 60 + now.minute


def is_fetch_window_allowed(now: datetime | None = None) -> bool:
    """True when historical OHLC fetch is allowed (07:00 <= t < 22:30 Europe/London)."""
    now = now or datetime.now(LONDON)
    minutes = _london_minutes(now)
    return FETCH_WINDOW_START_MINUTES <= minutes < FETCH_WINDOW_END_MINUTES


def in_ohlc_fetch_quiet_window(now: datetime | None = None) -> bool:
    """True during 22:30–07:00 Europe/London (live session REST budget protection)."""
    return not is_fetch_window_allowed(now)
