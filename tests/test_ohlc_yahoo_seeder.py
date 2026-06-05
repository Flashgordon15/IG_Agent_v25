"""Tests for data.ohlc_yahoo_seeder (offline validation helpers)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.ohlc_yahoo_seeder import validate_bars


def test_validate_bars_accepts_good_series() -> None:
    bars = []
    for i in range(120):
        hour = 10 + (i * 5) // 60
        minute = (i * 5) % 60
        bars.append(
            {
                "t": f"2026-01-01T{hour:02d}:{minute:02d}:00",
                "o": 1.1,
                "h": 1.2,
                "l": 1.0,
                "c": 1.15,
                "v": 0,
                "source": "yahoo",
            }
        )
    ok, msg = validate_bars(bars)
    assert ok, msg


def test_validate_bars_rejects_bad_ohlc() -> None:
    bars = [{"t": "2026-01-01T10:00:00", "o": 1.0, "h": 0.5, "l": 1.0, "c": 1.0}]
    ok, _ = validate_bars(bars)
    assert not ok
