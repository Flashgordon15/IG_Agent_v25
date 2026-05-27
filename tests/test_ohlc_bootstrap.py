"""Tests for IG OHLC bootstrap into SignalEngine."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock

from signals.signal_engine import SignalEngine
from system.config import Config
from trading.ohlc_bootstrap import bootstrap_ohlc_for_session


class OhlcBootstrapTests(unittest.TestCase):
    def test_injects_quotes_into_signal_engine(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        cfg.fast_ema = 9
        cfg.slow_ema = 21
        cfg.rsi_period = 14
        cfg.atr_period = 14
        engine = SignalEngine(cfg)
        rest = MagicMock()
        rest.fetch_price_history.return_value = [
            {
                "time": "2026-05-27T10:00:00",
                "high": 100.0,
                "low": 90.0,
                "bid_close": 94.0,
                "offer_close": 96.0,
                "close": 95.0,
            },
            {
                "time": "2026-05-27T10:05:00",
                "high": 102.0,
                "low": 92.0,
                "bid_close": 96.0,
                "offer_close": 98.0,
                "close": 97.0,
            },
        ]
        n = bootstrap_ohlc_for_session(rest, engine, "IX.D.NIKKEI.IFM.IP", "japan_225")
        self.assertEqual(n, 2)
        df = engine.quote_df("japan_225")
        self.assertEqual(len(df), 2)

    def test_rest_failure_does_not_raise(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        engine = SignalEngine(cfg)
        rest = MagicMock()
        rest.fetch_price_history.side_effect = RuntimeError("network down")
        n = bootstrap_ohlc_for_session(rest, engine, "EPIC", "mkt")
        self.assertEqual(n, 0)
        self.assertTrue(engine.quote_df("mkt").empty)

    def test_bar_count_positive_after_bootstrap(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        cfg.fast_ema = 9
        cfg.slow_ema = 21
        cfg.rsi_period = 14
        cfg.atr_period = 14
        engine = SignalEngine(cfg)
        bars = []
        for i in range(10):
            bars.append(
                {
                    "time": datetime(2026, 5, 27, 10, i * 5).isoformat(),
                    "high": 65000.0 + i,
                    "low": 64990.0 + i,
                    "bid_close": 64995.0 + i,
                    "offer_close": 65002.0 + i,
                    "close": 64998.0 + i,
                }
            )
        rest = MagicMock()
        rest.fetch_price_history.return_value = bars
        n = bootstrap_ohlc_for_session(rest, engine, "EPIC", "japan_225")
        self.assertGreater(n, 0)
        c5 = engine.candles(engine.quote_df("japan_225"), 5)
        self.assertGreater(len(c5), 0)


if __name__ == "__main__":
    unittest.main()
