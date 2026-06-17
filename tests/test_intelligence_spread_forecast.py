"""Unit tests — Dynamic Spread-Widening Forecast Model (mock tick series)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.spread_forecast import SpreadWideningForecast


class SpreadForecastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = SpreadWideningForecast(
            window=30,
            z_threshold=2.0,
            delta_z_threshold=1.5,
            min_samples=10,
        )

    def test_normal_spread_no_block(self) -> None:
        epic = "IX.D.NASDAQ.IFM.IP"
        for i in range(25):
            self.model.record(epic, 7.0 + (i % 3) * 0.1)
        v = self.model.compute(epic)
        self.assertFalse(v.blocked)
        self.assertLess(v.throttle_factor, 0.1)

    def test_widening_spread_triggers_throttle(self) -> None:
        epic = "CS.D.CFPGOLD.CFP.IP"
        for _ in range(20):
            self.model.record(epic, 0.45)
        for step in range(1, 12):
            self.model.record(epic, 0.45 + step * 0.15)
        v = self.model.compute(epic)
        self.assertGreater(v.z_score, 0.0)
        self.assertGreater(v.throttle_factor, 0.2)
        self.assertGreater(v.offset_widen_pts, 0.0)
        self.assertIn("spread_widening_forecast", v.reason)

    def test_insufficient_samples_safe_default(self) -> None:
        v = self.model.compute("EPIC.NEW")
        self.assertFalse(v.blocked)
        self.assertEqual(v.reason, "insufficient_samples")


if __name__ == "__main__":
    unittest.main()
