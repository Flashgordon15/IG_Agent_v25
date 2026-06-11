"""Rotation rank_score — direction-neutral trend cleanliness."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime.market_orchestrator import compute_rotation_trend_cleanliness


class RotationTrendCleanlinessTests(unittest.TestCase):
    def test_bull_and_bear_aligned_score_equally(self) -> None:
        bull = pd.Series({"fast_ema": 110.0, "slow_ema": 100.0, "rsi": 55.0})
        bear = pd.Series({"fast_ema": 100.0, "slow_ema": 110.0, "rsi": 45.0})
        bull_score = compute_rotation_trend_cleanliness(bull, atr_15m=10.0, atr_5m=10.0)
        bear_score = compute_rotation_trend_cleanliness(bear, atr_15m=10.0, atr_5m=10.0)
        self.assertAlmostEqual(bull_score, bear_score, places=4)
        self.assertGreater(bull_score, 20.0)

    def test_mixed_alignment_scores_below_full_trend(self) -> None:
        bear = pd.Series({"fast_ema": 100.0, "slow_ema": 110.0, "rsi": 45.0})
        mixed = pd.Series({"fast_ema": 100.0, "slow_ema": 110.0, "rsi": 55.0})
        bear_score = compute_rotation_trend_cleanliness(bear, atr_15m=10.0, atr_5m=10.0)
        mixed_score = compute_rotation_trend_cleanliness(
            mixed, atr_15m=10.0, atr_5m=10.0
        )
        self.assertGreater(bear_score, mixed_score)

    def test_wider_ema_gap_increases_momentum(self) -> None:
        tight = pd.Series({"fast_ema": 101.0, "slow_ema": 100.0, "rsi": 55.0})
        wide = pd.Series({"fast_ema": 120.0, "slow_ema": 100.0, "rsi": 55.0})
        tight_score = compute_rotation_trend_cleanliness(
            tight, atr_15m=10.0, atr_5m=10.0
        )
        wide_score = compute_rotation_trend_cleanliness(wide, atr_15m=10.0, atr_5m=10.0)
        self.assertGreater(wide_score, tight_score)


if __name__ == "__main__":
    unittest.main()
