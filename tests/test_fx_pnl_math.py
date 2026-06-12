"""FX contract pip value helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.pnl_math import fx_upl_per_ig_point


class TestFxPnlMath(unittest.TestCase):
    def test_usd_major_pip_value_scales_with_size(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        self.assertAlmostEqual(fx_upl_per_ig_point(epic, 5.0, currency="USD"), 50.0)
        self.assertAlmostEqual(fx_upl_per_ig_point(epic, 1.0, currency="USD"), 10.0)
        self.assertIsNone(fx_upl_per_ig_point("IX.D.DOW.IFM.IP", 5.0, currency="USD"))


if __name__ == "__main__":
    unittest.main()
