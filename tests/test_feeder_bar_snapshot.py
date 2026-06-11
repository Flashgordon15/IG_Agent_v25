"""Feeder bar_close extraction from signal snapshots."""

from __future__ import annotations

import pandas as pd

from trading.trading_loop import _feeder_bar_from_snapshot


def test_feeder_bar_from_pandas_series() -> None:
    last = pd.Series(
        {
            "time": "2026-06-08 12:25:00",
            "open": 1.1540,
            "high": 1.1545,
            "low": 1.1538,
            "close": 1.1542,
            "volume": 0,
        }
    )
    out = _feeder_bar_from_snapshot({"last": last})
    assert out is not None
    bar_time, ohlc = out
    assert "2026-06-08" in bar_time
    assert ohlc["close"] == 1.1542


def test_feeder_bar_from_dict() -> None:
    out = _feeder_bar_from_snapshot(
        {
            "last": {
                "time": "2026-06-08T12:25:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
            }
        }
    )
    assert out is not None
    _, ohlc = out
    assert ohlc["high"] == 101.0
