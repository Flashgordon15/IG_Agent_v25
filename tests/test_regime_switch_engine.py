"""Regime switch engine tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from runtime.regime_switch_engine import (
    RegimeState,
    _EpicEngine,
    evaluate_epic_regime,
    regime_allows_entry,
    reset_regime_switch_for_tests,
)
from signals.indicators import adx


def test_adx_computes_on_synthetic_trend():
    n = 120
    close = np.linspace(100, 130, n)
    high = close + 0.5
    low = close - 0.5
    df = pd.DataFrame({"high": high, "low": low, "close": close})
    series = adx(df, period=14)
    assert not series.empty
    assert float(series.iloc[-1]) >= 0


def test_chop_regime_blocks_entries_by_default():
    reset_regime_switch_for_tests()
    with patch.object(_EpicEngine, "_refresh_ohlc_ring_if_stale", return_value=0):
        snap = evaluate_epic_regime("IX.D.DOW.IFM.IP")
    assert snap.state == int(RegimeState.CHOP)
    assert snap.strategy_gate.get("allow_entries") is False


def test_hydrate_ring_with_fallback_zero_when_cache_empty():
    reset_regime_switch_for_tests()
    eng = _EpicEngine("IX.D.DOW.IFM.IP")
    with patch.object(_EpicEngine, "_refresh_ohlc_ring_if_stale", return_value=0):
        with patch("system.market_data_hub.get_market_data_hub") as hub:
            hub.return_value.get_snapshot.return_value = None
            row = eng.hydrate_ring_with_fallback()
    assert row["fallback"] is True
    assert row["source"] == "zero"
    assert row["bars"] == 0
    assert eng._count == 0


def test_hydrate_ring_with_fallback_uses_cache_at_288():
    reset_regime_switch_for_tests()
    eng = _EpicEngine("CS.D.CFPGOLD.CFP.IP")
    with patch.object(_EpicEngine, "_refresh_ohlc_ring_if_stale", return_value=288):
        row = eng.hydrate_ring_with_fallback()
    assert row["fallback"] is False
    assert row["source"] == "cache"
    assert row["bars"] == 288


def test_kalman_smoothing_reduces_whipsaw():
    eng = _EpicEngine("TEST.EPIC")
    v1 = eng._kalman.update(0.0)
    v2 = eng._kalman.update(2.0)
    v3 = eng._kalman.update(0.0)
    assert abs(v2 - v1) < abs(2.0 - v1)
    assert abs(v3 - v2) < 2.0
