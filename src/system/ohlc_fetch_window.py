"""Europe/London window for historical OHLC REST backfill (07:00–22:30)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")
_FETCH_WINDOW_START_MINUTES = 7 * 60  # 07:00 inclusive
_FETCH_WINDOW_END_MINUTES = 22 * 60 + 30  # 22:30 exclusive


def is_fetch_window_open(now: datetime | None = None) -> bool:
    """True when Europe/London time is 07:00 <= t < 22:30."""
    if now is None:
        now = datetime.now(_LONDON)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_LONDON)
    else:
        now = now.astimezone(_LONDON)
    minutes = now.hour * 60 + now.minute
    return _FETCH_WINDOW_START_MINUTES <= minutes < _FETCH_WINDOW_END_MINUTES
