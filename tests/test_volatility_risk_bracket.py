"""Unit tests for volatility-adjusted ATR risk bracket."""

from __future__ import annotations

from execution.volatility_risk_bracket import (
    BracketQuote,
    BracketState,
    dynamic_trail_atr_multiple,
    simulate_bracket_path,
    stop_hit,
    volatility_ratio,
)


def test_vol_ratio_clamps():
    assert volatility_ratio(0.0001, 0.0004) == 0.25
    assert volatility_ratio(0.0020, 0.0004) == 4.0


def test_dynamic_trail_tightens_in_high_vol():
    calm = dynamic_trail_atr_multiple(0.9)
    stressed = dynamic_trail_atr_multiple(2.0)
    assert stressed < calm


def test_flash_crash_long_triggers_stop():
    spread = 0.00005
    mids = [1.16000 + i * 0.00002 for i in range(40)]
    mids += [mids[-1] - 0.00120 for _ in range(10)]
    mids += [mids[-1] + 0.00015 for _ in range(70)]
    quotes = [
        BracketQuote(bid=round(m - spread, 5), offer=round(m + spread, 5))
        for m in mids
    ]
    state = BracketState.open_long(entry=1.16000, entry_atr=0.00040)
    sim = simulate_bracket_path(state, quotes)
    assert sim.stopped is True
    assert sim.stop_tick >= 40


def test_stop_hit_buy():
    assert stop_hit("BUY", 1.15900, bid=1.15890, offer=1.15900) is True
    assert stop_hit("BUY", 1.15900, bid=1.15910, offer=1.15920) is False
