"""Interim confidence scorer tests."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ml.interim_scorer import (
    InterimConfidenceScorer,
    invalidate_ml_clean_training_rows_cache,
    ml_clean_training_rows,
    ml_min_rows_for_model,
    should_use_interim_scorer,
)
from system.config import Config


def _cfg() -> Config:
    return Config(
        _data={
            "ml_min_rows_for_model": 50,
            "interim_scorer_weights": {
                "trend": 25,
                "session": 25,
                "volatility": 25,
                "recent_performance": 25,
            },
            "entry_protection": {"ml_min_rows_for_trust": 50},
        }
    )


class InterimScorerTests(unittest.TestCase):
    def test_interim_scorer_activates_below_50_rows(self) -> None:
        with patch("ml.interim_scorer.ml_clean_training_rows", return_value=14):
            with patch("trading.ml_scorer.get_ml_scorer") as mock_scorer:
                mock_scorer.return_value.is_trained.return_value = False
                self.assertTrue(should_use_interim_scorer(_cfg()))

    def test_interim_scorer_activates_when_model_missing_despite_rows(self) -> None:
        with patch("ml.interim_scorer.ml_clean_training_rows", return_value=102):
            with patch("trading.ml_scorer.get_ml_scorer") as mock_scorer:
                mock_scorer.return_value.is_trained.return_value = False
                self.assertTrue(should_use_interim_scorer(_cfg()))

    def test_ml_model_activates_at_50_rows_when_trained(self) -> None:
        with patch("ml.interim_scorer.ml_clean_training_rows", return_value=50):
            with patch("trading.ml_scorer.get_ml_scorer") as mock_scorer:
                mock_scorer.return_value.is_trained.return_value = True
                self.assertFalse(should_use_interim_scorer(_cfg()))

    def test_trend_score_strong_trend_returns_high(self) -> None:
        scorer = InterimConfidenceScorer()
        snap = {
            "last": {
                "fast_ema": 110,
                "slow_ema": 100,
                "atr": 5,
                "rsi": 62,
            },
            "trend15": {"fast_ema": 108, "slow_ema": 100},
            "atr_series": pd.Series([4, 5, 6, 5, 7] * 5),
        }
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 9, 0),
            )
        self.assertGreaterEqual(result.trend, 15)

    def test_trend_score_weak_trend_returns_low(self) -> None:
        scorer = InterimConfidenceScorer()
        snap = {
            "last": {"fast_ema": 100.2, "slow_ema": 100, "atr": 10, "rsi": 50},
            "trend15": {"fast_ema": 100.1, "slow_ema": 100},
            "atr_series": pd.Series([9, 10, 10, 9, 10] * 5),
        }
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 9, 0),
            )
        self.assertLessEqual(result.trend, 10)

    def test_session_score_london_overlap_returns_25(self) -> None:
        scorer = InterimConfidenceScorer()
        snap = {"last": {"fast_ema": 100, "slow_ema": 99, "atr": 5, "rsi": 55}}
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 14, 0),
            )
        self.assertEqual(result.session, 25)

    def test_session_score_asia_early_returns_10(self) -> None:
        scorer = InterimConfidenceScorer()
        snap = {"last": {"fast_ema": 100, "slow_ema": 99, "atr": 5, "rsi": 55}}
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 3, 0),
            )
        self.assertEqual(result.session, 10)

    def test_volatility_score_ideal_percentile_returns_25(self) -> None:
        scorer = InterimConfidenceScorer()
        series = pd.Series(list(range(1, 21)))
        snap = {
            "last": {"fast_ema": 101, "slow_ema": 100, "atr": 14, "rsi": 60},
            "atr_series": series,
        }
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 10, 0),
            )
        self.assertGreaterEqual(result.volatility, 10)

    def test_recent_performance_insufficient_data_returns_neutral(self) -> None:
        scorer = InterimConfidenceScorer()
        store = MagicMock()
        store.recent_confirmed_closed_trades.return_value = [{"result": "WIN"}]
        snap = {"last": {"fast_ema": 101, "slow_ema": 100, "atr": 5, "rsi": 60}}
        cfg = Config(
            _data={
                "interim_scorer": {"interim_scorer_min_recent_score": 20},
                "interim_scorer_weights": {
                    "trend": 25,
                    "session": 25,
                    "volatility": 25,
                    "recent_performance": 25,
                },
            }
        )
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=cfg,
                market="Gold",
                direction="BUY",
                snapshot=snap,
                store=store,
            )
        self.assertEqual(result.recent_performance, 20.0)

    def test_total_score_components_sum_correctly(self) -> None:
        scorer = InterimConfidenceScorer()
        snap = {
            "last": {"fast_ema": 110, "slow_ema": 100, "atr": 5, "rsi": 62},
            "trend15": {"fast_ema": 108, "slow_ema": 100},
            "atr_series": pd.Series([4, 5, 6, 5, 7] * 5),
        }
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 9, 0),
            )
        self.assertAlmostEqual(
            result.total,
            result.trend + result.session + result.volatility + result.recent_performance,
            places=1,
        )

    def test_interim_scorer_logs_component_breakdown(self) -> None:
        scorer = InterimConfidenceScorer()
        logs: list[str] = []
        snap = {"last": {"fast_ema": 101, "slow_ema": 100, "atr": 5, "rsi": 60}}
        with patch("ml.interim_scorer.log_engine", side_effect=logs.append):
            scorer.score(
                cfg=_cfg(),
                market="Gold",
                direction="BUY",
                snapshot=snap,
                now=datetime(2026, 6, 15, 10, 0),
            )
        self.assertTrue(any("[INTERIM SCORER]" in line for line in logs))

    def test_ml_min_rows_configurable(self) -> None:
        cfg = Config(_data={"ml_min_rows_for_model": 75})
        self.assertEqual(ml_min_rows_for_model(cfg), 75)

    def test_ml_clean_training_rows_session_cache(self) -> None:
        import time

        from ml import interim_scorer as ims
        from ml.interim_scorer import reset_ml_clean_training_rows_cache_for_tests

        reset_ml_clean_training_rows_cache_for_tests()
        cfg = _cfg()
        key = ims._ml_clean_training_rows_cache_key(cfg)
        with ims._ml_clean_training_rows_lock:
            ims._ml_clean_training_rows_cache[key] = 42
            ims._ml_clean_training_rows_stale[key] = 42
        with patch(
            "ml.interim_scorer._query_ml_clean_training_rows", return_value=99
        ) as query_mock:
            self.assertEqual(ml_clean_training_rows(cfg), 42)
            self.assertEqual(ml_clean_training_rows(cfg), 42)
            self.assertEqual(query_mock.call_count, 0)
            invalidate_ml_clean_training_rows_cache()
            self.assertEqual(ml_clean_training_rows(cfg), 42)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with ims._ml_clean_training_rows_lock:
                    if ims._ml_clean_training_rows_cache.get(key) == 99:
                        break
                time.sleep(0.02)
            self.assertEqual(ml_clean_training_rows(cfg), 99)
            self.assertEqual(query_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
