"""Tests for environment_scorer — factors, caps, bands, safe default."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from trading.environment_scorer import (
    SAFE_DEFAULT_SCORE,
    EnvironmentScorer,
    regime_label,
    score_atr_factor,
    score_session_timing_factor,
    score_spread_factor,
    score_trend_factor,
)


class FactorUnitTests(unittest.TestCase):
    def test_atr_factor_bands(self) -> None:
        self.assertEqual(score_atr_factor(10, 10), 30.0)
        self.assertEqual(score_atr_factor(4, 10), 0.0)
        self.assertEqual(score_atr_factor(20, 10), 0.0)
        mid = score_atr_factor(13, 10)
        self.assertGreater(mid, 0)
        self.assertLess(mid, 30)

    def test_trend_factor(self) -> None:
        strong = pd.Series({"fast_ema": 110, "slow_ema": 100, "rsi": 55})
        partial = pd.Series({"fast_ema": 110, "slow_ema": 100, "rsi": 45})
        flat = pd.Series({"fast_ema": 100, "slow_ema": 110, "rsi": 45})
        self.assertEqual(score_trend_factor(strong), 25.0)
        self.assertEqual(score_trend_factor(partial), 12.5)
        self.assertEqual(score_trend_factor(flat), 0.0)

    def test_session_timing_factor(self) -> None:
        self.assertEqual(
            score_session_timing_factor(datetime(2026, 5, 27, 1, 0)), 20.0
        )
        self.assertEqual(
            score_session_timing_factor(datetime(2026, 5, 27, 4, 0)), 15.0
        )
        self.assertEqual(
            score_session_timing_factor(datetime(2026, 5, 27, 6, 45)), 5.0
        )
        self.assertEqual(
            score_session_timing_factor(datetime(2026, 5, 27, 12, 0)), 0.0
        )

    def test_spread_factor(self) -> None:
        self.assertEqual(score_spread_factor(10, 10), 25.0)
        self.assertEqual(score_spread_factor(25, 10), 0.0)
        mid = score_spread_factor(16, 10)
        self.assertGreater(mid, 0)
        self.assertLess(mid, 25)

    def test_regime_labels(self) -> None:
        self.assertEqual(regime_label(85), "Excellent")
        self.assertEqual(regime_label(70), "Good")
        self.assertEqual(regime_label(45), "Marginal")
        self.assertEqual(regime_label(30), "WAIT")


def _bar_frame(n: int, *, spread: float = 0.5) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append(
            {
                "time": datetime(2026, 5, 27, 0, 0) + timedelta(minutes=i * 5),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "price": 100.5,
                "bid": 100.0,
                "offer": 100.0 + spread,
                "spread": spread,
                "fast_ema": 101.0,
                "slow_ema": 99.0,
                "rsi": 55.0,
                "atr": 10.0,
            }
        )
    return pd.DataFrame(rows)


def _make_engine_with_bars(n_5m: int = 25) -> MagicMock:
    engine = MagicMock()
    df = _bar_frame(min(n_5m * 3, 60), spread=0.5)
    c5 = _bar_frame(n_5m, spread=0.5)
    c15 = _bar_frame(max(3, n_5m // 3), spread=0.5)

    engine.quote_df.return_value = df
    engine.candles.side_effect = lambda _df, minutes: c5 if minutes == 5 else c15
    engine.add_indicators.side_effect = lambda frame: frame
    cfg = MagicMock()
    cfg.max_spread_points = 35.0
    engine.config = cfg
    return engine


class EnvironmentScorerIntegrationTests(unittest.TestCase):
    def test_score_returns_all_factors(self) -> None:
        engine = _make_engine_with_bars()
        scorer = EnvironmentScorer(engine, normal_spread=7.0)
        scorer.reset_session("Japan 225")
        scorer._bars_at_session_open["Japan 225"] = 0
        total = scorer.score("Japan 225")
        factors = scorer.get_factors()
        self.assertEqual(
            set(factors.keys()),
            {"atr", "trend", "session", "spread", "sentiment"},
        )
        numeric = {k: float(v) for k, v in factors.items() if k != "sentiment"}
        self.assertAlmostEqual(sum(numeric.values()), total, places=4)
        self.assertIn(scorer.get_regime(), ("Excellent", "Good", "Marginal", "WAIT"))

    def test_cold_start_cap(self) -> None:
        engine = _make_engine_with_bars(n_5m=3)
        scorer = EnvironmentScorer(engine, normal_spread=7.0)
        scorer.reset_session("Japan 225")
        total = scorer.score("Japan 225")
        self.assertLessEqual(total, 40.0)
        self.assertTrue(scorer.last_score().capped_cold_start)

    def test_gap_open_cap(self) -> None:
        engine = _make_engine_with_bars()
        scorer = EnvironmentScorer(engine, normal_spread=7.0)
        scorer.reset_session("Japan 225")
        scorer.register_gap_open("Japan 225")
        total = scorer.score("Japan 225")
        self.assertLessEqual(total, 40.0)
        self.assertTrue(scorer.last_score().capped_gap_open)

    def test_safe_default_on_error(self) -> None:
        scorer = EnvironmentScorer(None)
        with patch.object(
            EnvironmentScorer, "_compute_factors", side_effect=RuntimeError("fail")
        ):
            total = scorer.score("Japan 225")
        self.assertEqual(total, SAFE_DEFAULT_SCORE)
        self.assertEqual(scorer.get_regime(), "Marginal")
        self.assertTrue(scorer.last_score().gate_passes)

    def test_gate_pass_marginal_band(self) -> None:
        engine = _make_engine_with_bars()
        scorer = EnvironmentScorer(engine, normal_spread=7.0)
        scorer.reset_session("Japan 225")
        scorer._bars_at_session_open["Japan 225"] = 0
        with patch.object(
            EnvironmentScorer,
            "_compute_factors",
            return_value=(
                {"atr": 10, "trend": 10, "session": 10, "spread": 12},
                {"complete_bars": 20},
            ),
        ):
            total = scorer.score("Japan 225")
        self.assertEqual(total, 42.0)
        self.assertEqual(scorer.get_regime(), "Marginal")
        self.assertTrue(total >= 40.0)


if __name__ == "__main__":
    unittest.main()
