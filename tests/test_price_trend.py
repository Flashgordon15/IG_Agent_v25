"""Tests for trading.price_trend — 30m mid trend helper."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.price_trend import (
    FLAT_POINTS,
    FLAT_PCT,
    compute_price_trend_30m,
)


def _quotes(
    start_mid: float,
    end_mid: float,
    *,
    n: int = 12,
    step_min: int = 2,
    base: datetime | None = None,
) -> pd.DataFrame:
    base = base or datetime(2026, 5, 27, 10, 0, 0)
    rows = []
    for i in range(n):
        t = base + timedelta(minutes=i * step_min)
        mid = start_mid + (end_mid - start_mid) * i / max(1, n - 1)
        spread = 0.5
        rows.append(
            {
                "time": t,
                "bid": mid - spread / 2,
                "offer": mid + spread / 2,
                "mid": mid,
                "spread": spread,
            }
        )
    return pd.DataFrame(rows)


class PriceTrendTests(unittest.TestCase):
    def test_up_trend(self) -> None:
        end = datetime(2026, 5, 27, 10, 30, 0)
        df = _quotes(39000.0, 39100.0, n=20, step_min=1, base=end - timedelta(minutes=25))
        out = compute_price_trend_30m(df, now=end)
        assert out is not None
        self.assertEqual(out["direction"], "up")
        self.assertGreater(out["change_pts"], FLAT_POINTS)

    def test_down_trend(self) -> None:
        end = datetime(2026, 5, 27, 10, 30, 0)
        df = _quotes(39100.0, 38950.0, n=20, step_min=1, base=end - timedelta(minutes=25))
        out = compute_price_trend_30m(df, now=end)
        assert out is not None
        self.assertEqual(out["direction"], "down")
        self.assertLess(out["change_pts"], -FLAT_POINTS)

    def test_flat_within_band(self) -> None:
        end = datetime(2026, 5, 27, 10, 30, 0)
        df = _quotes(39000.0, 39005.0, n=16, step_min=1, base=end - timedelta(minutes=20))
        out = compute_price_trend_30m(df, now=end)
        assert out is not None
        self.assertEqual(out["direction"], "flat")
        self.assertLess(abs(out["change_pts"]), FLAT_POINTS)
        self.assertLess(abs(out["change_pct"] / 100.0), FLAT_PCT + 1e-6)

    def test_insufficient_data(self) -> None:
        end = datetime(2026, 5, 27, 10, 30, 0)
        df = _quotes(39000.0, 39010.0, n=1, base=end)
        self.assertIsNone(compute_price_trend_30m(df, now=end))

    def test_empty_frame(self) -> None:
        self.assertIsNone(compute_price_trend_30m(pd.DataFrame()))


if __name__ == "__main__":
    unittest.main()
