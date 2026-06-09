"""Tests for IG OHLC bootstrap into SignalEngine."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.config import Config
from trading.ohlc_bootstrap import (
    MIN_CACHE_BARS_FOR_BOOTSTRAP,
    _parse_bar_time,
    bootstrap_ohlc_for_session,
    clear_historical_allowance_lockout_for_tests,
    mark_historical_allowance_lockout,
)


class OhlcBootstrapTests(unittest.TestCase):
    def test_parse_ig_snapshot_time_format(self) -> None:
        dt = _parse_bar_time("2026/05/28:14:30:00")
        self.assertEqual(dt, datetime(2026, 5, 28, 14, 30, 0))

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
        with patch("trading.ohlc_bootstrap.local_cache_ready", return_value=False):
            n = bootstrap_ohlc_for_session(
                rest, engine, "IX.D.NIKKEI.IFM.IP", "japan_225", prefer_cache=False
            )
        self.assertEqual(n, 2)
        self.assertEqual(engine.ohlc_seed_count("japan_225"), 2)
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

    def test_ig_times_produce_multiple_candles(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        cfg.fast_ema = 9
        cfg.slow_ema = 21
        cfg.rsi_period = 14
        cfg.atr_period = 14
        engine = SignalEngine(cfg)
        bars = []
        base = datetime(2026, 5, 27, 10, 0)
        for i in range(20):
            t = base + timedelta(minutes=5 * i)
            h = 10 + i
            bars.append(
                {
                    "time": f"{t.year}/{t.month:02d}/{t.day:02d}:{t.hour:02d}:{t.minute:02d}:00",
                    "high": float(100 + h),
                    "low": float(90 + h),
                    "bid_close": 94.0 + h,
                    "offer_close": 96.0 + h,
                    "close": 95.0 + h,
                }
            )
        rest = MagicMock()
        rest.fetch_price_history.return_value = bars
        n = bootstrap_ohlc_for_session(rest, engine, "EPIC", "Japan 225")
        self.assertEqual(n, 20)
        c5 = engine.candles(engine.quote_df("Japan 225"), 5)
        self.assertGreaterEqual(len(c5), 10)

    def test_seed_registered_under_epic_alias(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        engine = SignalEngine(cfg)
        rest = MagicMock()
        rest.fetch_price_history.return_value = [
            {
                "time": "2026/05/28:14:30:00",
                "high": 110.0,
                "low": 100.0,
                "bid_close": 105.0,
                "offer_close": 106.0,
                "close": 105.0,
            },
            {
                "time": "2026/05/28:14:35:00",
                "high": 111.0,
                "low": 101.0,
                "bid_close": 106.0,
                "offer_close": 107.0,
                "close": 106.0,
            },
        ]
        with patch("trading.ohlc_bootstrap.local_cache_ready", return_value=False):
            bootstrap_ohlc_for_session(
                rest, engine, "IX.D.NIKKEI.IFM.IP", "Japan 225", prefer_cache=False
            )
        self.assertEqual(engine.ohlc_seed_count("IX.D.NIKKEI.IFM.IP"), 2)
        c5 = engine.candles(engine.quote_df("IX.D.NIKKEI.IFM.IP"), 5)
        self.assertGreaterEqual(len(c5), 2)

    def test_hundred_ig_bars_yield_many_candles(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        cfg.fast_ema = 9
        cfg.slow_ema = 21
        cfg.rsi_period = 14
        cfg.atr_period = 14
        engine = SignalEngine(cfg)
        base = datetime(2026, 5, 27, 10, 0)
        bars = []
        for i in range(100):
            t = base + timedelta(minutes=5 * i)
            bars.append(
                {
                    "time": f"{t.year}/{t.month:02d}/{t.day:02d}:{t.hour:02d}:{t.minute:02d}:00",
                    "high": 110.0 + i * 0.1,
                    "low": 100.0 + i * 0.1,
                    "bid_close": 105.0,
                    "offer_close": 106.0,
                    "close": 105.0,
                }
            )
        rest = MagicMock()
        rest.fetch_price_history.return_value = bars
        n = bootstrap_ohlc_for_session(rest, engine, "EPIC", "Japan 225")
        self.assertEqual(n, 100)
        _, c5, c15, c60 = engine.candle_frames("Japan 225")
        self.assertGreaterEqual(len(c5), 20)
        self.assertGreaterEqual(len(c15), 6)
        self.assertGreaterEqual(len(c60), 1)

    def test_ohlc_seed_survives_live_tick_trim(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 50
        cfg.fast_ema = 9
        cfg.slow_ema = 21
        cfg.rsi_period = 14
        cfg.atr_period = 14
        engine = SignalEngine(cfg)
        base = datetime(2026, 5, 27, 10, 0)
        bars = []
        for i in range(15):
            t = base + timedelta(minutes=5 * i)
            bars.append(
                {
                    "time": f"{t.year}/{t.month:02d}/{t.day:02d}:{t.hour:02d}:{t.minute:02d}:00",
                    "high": 110.0,
                    "low": 100.0,
                    "bid_close": 105.0,
                    "offer_close": 106.0,
                    "close": 105.0,
                }
            )
        rest = MagicMock()
        rest.fetch_price_history.return_value = bars
        bootstrap_ohlc_for_session(rest, engine, "EPIC", "mkt")
        for j in range(200):
            engine.add_quote(
                "mkt", Quote(datetime(2026, 5, 28, 12, 0, j % 60), 100.0, 101.0)
            )
        c5 = engine.candles(engine.quote_df("mkt"), 5)
        self.assertGreaterEqual(len(c5), 10)

    def test_historical_lockout_uses_local_cache_without_ig(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        cfg = MagicMock(spec=Config)
        cfg.max_live_quotes = 5000
        cfg.fast_ema = 9
        cfg.slow_ema = 21
        cfg.rsi_period = 14
        cfg.atr_period = 14
        engine = SignalEngine(cfg)
        clear_historical_allowance_lockout_for_tests()
        bars = []
        base = datetime(2026, 5, 27, 10, 0)
        for i in range(120):
            t = base + timedelta(minutes=5 * i)
            bars.append(
                {
                    "t": t.isoformat(),
                    "o": 100.0 + i,
                    "h": 101.0 + i,
                    "l": 99.0 + i,
                    "c": 100.5 + i,
                    "v": 1.0,
                    "spread": 1.0,
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "nikkei_5m.jsonl"
            cache.write_text(
                "\n".join(json.dumps(b) for b in bars) + "\n", encoding="utf-8"
            )
            with patch("trading.ohlc_bootstrap.ohlc_cache_path", return_value=cache):
                mark_historical_allowance_lockout(source="test")
                rest = MagicMock()
                rest.fetch_price_history.return_value = []
                n = bootstrap_ohlc_for_session(
                    rest,
                    engine,
                    "IX.D.NIKKEI.IFM.IP",
                    "Japan 225",
                    prefer_cache=True,
                )
        clear_historical_allowance_lockout_for_tests()
        self.assertGreaterEqual(n, MIN_CACHE_BARS_FOR_BOOTSTRAP)
        c5 = engine.candles(engine.quote_df("Japan 225"), 5)
        ind = engine.add_indicators(c5)
        self.assertGreater(float(ind.iloc[-1]["fast_ema"]), 0)
        rest.fetch_price_history.assert_not_called()

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
