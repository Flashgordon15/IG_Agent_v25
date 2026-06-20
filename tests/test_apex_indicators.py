"""v30 Apex vectorized indicator kernel tests."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signals.indicators import (  # noqa: E402
    ML_VETO_FLOOR,
    RSI_MIN_HISTORY_BARS,
    apply_indicators_frame,
    atr,
    build_validation_mask,
    compute_last_bar_indicators,
    compute_math_matrix,
    ema,
    rsi,
)


class VectorizedIndicatorTests(unittest.TestCase):
    def test_rsi_length_guard_neutral(self) -> None:
        short = pd.Series([1.0, 2.0, 3.0])
        out = rsi(short, period=14)
        self.assertEqual(len(out), 3)
        self.assertTrue((out == 50.0).all())

    def test_rsi_clips_outliers(self) -> None:
        prices = pd.Series(np.linspace(100, 200, RSI_MIN_HISTORY_BARS + 5))
        out = rsi(prices, period=14)
        self.assertGreaterEqual(float(out.iloc[-1]), 15.0)
        self.assertLessEqual(float(out.iloc[-1]), 85.0)

    def test_apply_indicators_frame_columns(self) -> None:
        n = 40
        df = pd.DataFrame(
            {
                "price": np.linspace(100, 120, n),
                "high": np.linspace(101, 121, n),
                "low": np.linspace(99, 119, n),
                "close": np.linspace(100, 120, n),
            }
        )
        out = apply_indicators_frame(df)
        for col in ("rsi", "fast_ema", "slow_ema", "atr"):
            self.assertIn(col, out.columns)
            self.assertFalse(out[col].isna().all())

    def test_last_bar_hot_path_under_250us_median(self) -> None:
        n = 500
        close = np.linspace(4300, 4320, n, dtype=np.float64)
        high = close + 0.5
        low = close - 0.5
        samples = []
        for _ in range(30):
            snap = compute_last_bar_indicators(close, high, low)
            samples.append(snap["elapsed_us"])
        median_us = float(np.median(samples))
        self.assertLess(median_us, 2500.0)  # CI headroom; local target <250µs

    def test_pandas_wrappers_match_shapes(self) -> None:
        s = pd.Series(np.linspace(1, 50, 30))
        self.assertEqual(len(ema(s, 12)), 30)
        self.assertEqual(len(rsi(s, 14)), 30)
        df = pd.DataFrame(
            {
                "high": s + 1,
                "low": s - 1,
                "close": s,
            }
        )
        self.assertEqual(len(atr(df, 14)), 30)

    def test_math_matrix_float64_and_validation_mask(self) -> None:
        n = 40
        close = np.linspace(4300.0, 4320.0, n, dtype=np.float64)
        high = close + 0.5
        low = close - 0.5
        snap = compute_math_matrix(close, high, low, ml_probability=0.52)
        self.assertEqual(snap["close"].dtype, np.float64)
        self.assertEqual(snap["indicator_matrix"].dtype, np.float64)
        self.assertEqual(snap["indicator_matrix"].shape, (n, 4))
        self.assertEqual(len(snap["validation_mask"]), n)
        self.assertGreaterEqual(float(snap["ml_veto_floor"]), ML_VETO_FLOOR - 0.01)
        self.assertTrue(bool(snap["ml_pass"]))

    def test_validation_mask_blocks_sub_floor_ml(self) -> None:
        n = 30
        rsi = np.full(n, 55.0, dtype=np.float64)
        mask = build_validation_mask(
            n, rsi, ml_probability=0.40, ml_veto_floor=ML_VETO_FLOOR
        )
        self.assertFalse(bool(mask[-1]))

    def test_history_guard_blocks_thin_window(self) -> None:
        close = np.linspace(100.0, 110.0, RSI_MIN_HISTORY_BARS - 2, dtype=np.float64)
        snap = compute_math_matrix(close)
        self.assertFalse(snap["history_ok"])
        self.assertFalse(snap["ml_pass"])


if __name__ == "__main__":
    unittest.main()
