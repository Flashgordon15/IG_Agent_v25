"""Tests for S2 per-epic threshold tuner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research import s2_threshold_tuner as tuner


def test_tune_epic_picks_threshold_near_target() -> None:
    bars = []
    for i in range(100):
        # Wide range bars — low threshold fires more
        bars.append(
            {
                "t": f"2026-06-08T10:{i % 60:02d}:00",
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.9 if i % 2 == 0 else 99.1,
            }
        )
    result = tuner.tune_epic(
        epic="IX.D.NIKKEI.IFM.IP",
        market="Japan 225",
        bars=bars,
    )
    assert result["bars"] == 100
    assert result["min_range_pct"] in tuner._SWEEP
    assert result["would_trade"] >= 0


def test_s2_momentum_honours_min_range_override() -> None:
    from strategies.s2_momentum import S2Momentum

    event = {
        "event_type": "bar_close",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "market": "Japan 225",
        "session": "london_morning",
        "ts": "2026-06-08T10:00:00Z",
        "payload": {
            "open": 100.0,
            "high": 100.2,
            "low": 99.9,
            "close": 100.15,
        },
    }
    loose = S2Momentum(min_range_pct=0.0001).evaluate_feeder_event(event)
    strict = S2Momentum(min_range_pct=0.05).evaluate_feeder_event(event)
    assert loose is not None
    assert strict is None
