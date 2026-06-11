"""Tests for v26 shadow P&L attribution."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "v26"))

from expectancy.shadow_attribution import (
    _direction_from_payload,
    attribute_fills,
)


def test_direction_from_setup_key_when_fill_omits_side() -> None:
    payload = {
        "setup_key": "SELL|bear|asia_early|atr180-210|rsilow|volnormal",
        "pnl_gbp": -22.7,
    }
    assert _direction_from_payload(payload) == "SELL"


def test_attribute_fill_matches_by_setup_key() -> None:
    fill = {
        "ts": "2026-06-08T01:32:22Z",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "payload": {
            "setup_key": "SELL|bear|asia_early|atr180-210|rsilow|volnormal",
            "pnl_gbp": -52.6,
            "result": "LOSS",
        },
    }
    shadow = {
        "ts": "2026-06-08T01:25:06Z",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "strategy_id": "S1_rules_v25",
        "payload": {
            "direction": "SELL",
            "would_trade": True,
            "setup_key": "SELL|bear|asia_early|atr180-210|rsilow|volnormal",
        },
    }
    attributed = attribute_fills([fill], [shadow])
    assert len(attributed) == 1
    assert attributed[0].strategy_id == "S1_rules_v25"
    assert attributed[0].pnl_gbp == -52.6
    assert (
        attributed[0].lag_sec
        == (
            datetime(2026, 6, 8, 1, 32, 22, tzinfo=timezone.utc)
            - datetime(2026, 6, 8, 1, 25, 6, tzinfo=timezone.utc)
        ).total_seconds()
    )


def test_attribute_skips_when_direction_conflicts() -> None:
    fill = {
        "ts": "2026-06-08T02:00:00Z",
        "epic": "CS.D.CFPGOLD.CFP.IP",
        "payload": {
            "setup_key": "BUY|bull|london_morning|atr0-30|rsimid|volnormal",
            "pnl_gbp": 10.0,
            "result": "WIN",
        },
    }
    shadow = {
        "ts": "2026-06-08T01:55:00Z",
        "epic": "CS.D.CFPGOLD.CFP.IP",
        "strategy_id": "S1_rules_v25",
        "payload": {
            "direction": "SELL",
            "would_trade": True,
            "setup_key": "SELL|bear|london_morning|atr0-30|rsimid|volnormal",
        },
    }
    assert attribute_fills([fill], [shadow]) == []
