"""v28 spec parity — shadow learning, 1h regime, unified ATR gate risk."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai.strategy.performance_reviewer import (
    process_shadow_learning_pipeline,
    simulate_shadow_outcome,
)
from data.learning_store import LearningStore
from data.models import Quote
from signals.signal_engine import SignalEngine
from trading.trading_loop import TradingLoop


def _cfg_mock(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.max_live_quotes = 5000
    cfg.fast_ema = 9
    cfg.slow_ema = 21
    cfg.rsi_period = 14
    cfg.atr_period = 14
    cfg.signal_threshold = 55
    cfg.rsi_buy_min = 45
    cfg.rsi_buy_max = 70
    cfg.rsi_sell_min = 30
    cfg.rsi_sell_max = 55
    cfg.max_spread_points = 50
    cfg.min_atr_points = 0
    cfg.max_atr_points = 0
    cfg.vol_regime_filter_enabled = False
    cfg.momentum_gap_points = 5
    cfg.learning_enabled = False
    cfg.learning_min_trades_per_setup = 5
    cfg.learning_max_bonus = 5
    cfg.learning_max_penalty = 5
    cfg.adaptive_good_winrate_threshold = 0.55
    cfg.adaptive_bad_winrate_threshold = 0.45
    for key, val in overrides.items():
        setattr(cfg, key, val)
    return cfg


class ShadowLearningPipelineTests(unittest.TestCase):
    def test_simulate_shadow_outcome_buy_win(self) -> None:
        forward = [
            {"high": 110, "low": 105, "close": 108},
            {"high": 155, "low": 112, "close": 150},
        ]
        result, pnl = simulate_shadow_outcome(
            side="BUY",
            entry=100.0,
            atr_pts=10.0,
            forward_bars=forward,
            stop_mult=2.5,
            reward_mult=2.0,
            stop_floor=0,
            stop_cap=999,
        )
        self.assertEqual(result, "WIN")
        self.assertEqual(pnl, 50.0)

    def test_pipeline_ingests_skipped_shadow_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shadow = tmp_path / "shadow_log.jsonl"
            shadow.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-09T10:00:00",
                        "market": "Japan 225",
                        "direction": "SELL",
                        "adjusted_score": 72.0,
                        "would_have_fired": False,
                        "gate_blocked_at": "signal_confidence",
                        "setup_key": "SELL|bear|london_morning|atr30-60|rsimid|volnormal",
                        "atr": 10.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ohlc_dir = tmp_path / "ohlc_cache"
            ohlc_dir.mkdir()
            bars = []
            base = datetime(2026, 6, 9, 8, 0, tzinfo=timezone.utc)
            price = 200.0
            for i in range(30):
                t = base + timedelta(minutes=5 * i)
                if i >= 24:
                    price -= 3.0
                bars.append(
                    {
                        "time": t.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": price,
                        "high": price + 2,
                        "low": price - 15,
                        "close": price,
                    }
                )
            (ohlc_dir / "nikkei_5m.jsonl").write_text(
                "\n".join(json.dumps(b) for b in bars) + "\n",
                encoding="utf-8",
            )
            db_path = tmp_path / "learning.db"
            store = LearningStore(str(db_path))
            store.connect()

            with (
                patch(
                    "ai.strategy.performance_reviewer.data_dir",
                    return_value=tmp_path,
                ),
                patch(
                    "ai.strategy.performance_reviewer.ohlc_cache_path",
                    lambda epic, market="": ohlc_dir / "nikkei_5m.jsonl",
                ),
                patch(
                    "ai.strategy.performance_reviewer._market_epic_map",
                    return_value={"Japan 225": "IX.D.NIKKEI.IFM.IP"},
                ),
            ):
                result = process_shadow_learning_pipeline(
                    store,
                    shadow_path=shadow,
                    persist_offset=False,
                )

            self.assertGreaterEqual(result.ingested, 1)
            stats = store.setup_stats(
                "SELL|bear|london_morning|atr30-60|rsimid|volnormal"
            )
            self.assertIsNotNone(stats)
            assert stats is not None
            self.assertGreaterEqual(int(stats["trades"]), 1)
            store.close()


class OneHourRegimeTests(unittest.TestCase):
    def _seed_uptrend_engine(self) -> SignalEngine:
        cfg = _cfg_mock(signal_threshold=50)
        engine = SignalEngine(cfg)
        base = datetime(2026, 6, 1, 8, 0)
        quotes: list[Quote] = []
        price = 100.0
        for i in range(1800):
            t = base + timedelta(minutes=i)
            if i > 1700:
                price -= 0.15
            else:
                price += 0.05
            quotes.append(Quote(t, price - 0.1, price + 0.1))
        engine.seed_ohlc_history("Test", quotes)
        return engine

    def test_candle_frames_include_60m(self) -> None:
        engine = self._seed_uptrend_engine()
        _, c5, c15, c60 = engine.candle_frames("Test")
        self.assertGreater(len(c5), 0)
        self.assertGreater(len(c15), 0)
        self.assertGreater(len(c60), 0)

    def test_sell_blocked_without_1h_bearish_regime(self) -> None:
        cfg = _cfg_mock(signal_threshold=50, rsi_sell_min=0, rsi_sell_max=100)
        engine = SignalEngine(cfg)
        base = datetime(2026, 6, 1, 8, 0)
        quotes: list[Quote] = []
        price = 100.0
        for i in range(1800):
            t = base + timedelta(minutes=i)
            if i > 1700:
                price -= 0.15
            else:
                price += 0.05
            quotes.append(Quote(t, price - 0.1, price + 0.1))
        engine.seed_ohlc_history("Test", quotes)
        result = engine.evaluate("Test")
        snap = engine.last_snapshot.get("Test", {})
        self.assertFalse(snap.get("h1_bearish", True))
        self.assertEqual(result.signal, "WAIT")
        self.assertTrue(snap.get("h1_block") or "1h" in result.notes.lower())


class UnifiedGateRiskTests(unittest.TestCase):
    def test_gate_risk_uses_adaptive_stop_not_config_fixed(self) -> None:
        from tests.test_trading_loop import _make_loop, _quote

        loop = _make_loop()
        loop._epic = "IX.D.DOW.IFM.IP"
        loop._market = "Wall Street"
        loop._config.stop_distance_points = 80.0
        loop._config.trade_size = 1.0
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": 500,
            }.get(key, default)
        )
        loop._points.get_size_multiplier.return_value = 1.0
        loop._execution_loop.execution_engine._adaptive.settings.side_effect = (
            lambda *a, **k: {"risk": 25.0}
        )
        loop._signal_engine.last_snapshot = {
            "Wall Street": {"last": {"atr": 10.0, "spread": 1.0}}
        }
        loop._signal_engine.evaluate.return_value = MagicMock(
            adjusted_confidence=80.0,
            setup_key="BUY|bull|london_morning|atr30-60|rsimid|volnormal",
        )
        rest = MagicMock()
        rest.fetch_market_constraints.return_value = {"min_deal_size": 1.0}
        loop._execution_loop.execution_engine._rest_client = rest

        with (
            patch("system.market_data_hub.get_market_data_hub") as hub_mock,
            patch("system.risk_bands.bands_enabled", return_value=False),
        ):
            hub_mock.return_value.normal_spread.return_value = 5.0
            gate = loop._gate_risk_validation(_quote())

        self.assertTrue(gate.passed)
        self.assertEqual(gate.value["stop_points"], 25.0)
        self.assertEqual(gate.value["stop_source"], "adaptive_atr")
        self.assertEqual(gate.value["risk_gbp"], 25.0)


if __name__ == "__main__":
    unittest.main()
