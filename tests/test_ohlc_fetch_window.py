from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from system.ohlc_fetch_window import (
    FETCH_WINDOW_END_MINUTES,
    FETCH_WINDOW_START_MINUTES,
    in_ohlc_fetch_quiet_window,
    is_fetch_window_allowed,
)

LONDON = ZoneInfo("Europe/London")


def _london(hour: int, minute: int) -> datetime:
    return datetime(2026, 5, 28, hour, minute, tzinfo=LONDON)


def test_fetch_window_boundaries() -> None:
    assert FETCH_WINDOW_START_MINUTES == 7 * 60
    assert FETCH_WINDOW_END_MINUTES == 22 * 60 + 30
    assert not is_fetch_window_allowed(_london(6, 59))
    assert is_fetch_window_allowed(_london(7, 0))
    assert is_fetch_window_allowed(_london(12, 0))
    assert is_fetch_window_allowed(_london(22, 29))
    assert not is_fetch_window_allowed(_london(22, 30))
    assert not is_fetch_window_allowed(_london(23, 0))
    assert in_ohlc_fetch_quiet_window(_london(22, 30))
    assert not in_ohlc_fetch_quiet_window(_london(7, 0))
