from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from system.ohlc_fetch_window import is_fetch_window_open

LONDON = ZoneInfo("Europe/London")


def _london(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


def test_inside_window() -> None:
    assert is_fetch_window_open(_london(2026, 5, 28, 12, 0)) is True


def test_outside_window_early_morning() -> None:
    assert is_fetch_window_open(_london(2026, 5, 28, 3, 0)) is False


def test_exactly_0700() -> None:
    assert is_fetch_window_open(_london(2026, 5, 28, 7, 0)) is True


def test_exactly_2230() -> None:
    assert is_fetch_window_open(_london(2026, 5, 28, 22, 30)) is False


def test_dst_spring_forward_boundary() -> None:
    # Europe/London spring forward 2026-03-29: 01:00 GMT -> 02:00 BST (no local 01:xx).
    # 07:00 BST on transition day is still inside the fetch window.
    assert is_fetch_window_open(_london(2026, 3, 29, 7, 0)) is True
