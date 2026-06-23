"""Iron Clad Risk Engine tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from datetime import datetime, timezone
from harmonization.iron_clad_risk import IronCladRiskEngine, enforce_slippage_on_fill
from harmonization.tick_integrity import TickIntegrityFilter, reset_tick_integrity_for_tests
from harmonization.volatility_gate import dynamic_confidence_floor


class HarmonizationTests(unittest.TestCase):
    def setUp(self) -> None:
        IronCladRiskEngine.reset_for_tests()
        reset_tick_integrity_for_tests()

    def test_rejects_oversized_order(self) -> None:
        ok, reason, _ = IronCladRiskEngine.validate_order(
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            size=2.0,
            stop_distance=10,
            limit_distance=20,
            bid=1.08,
            offer=1.0805,
            rest_client=MagicMock(),
        )
        self.assertFalse(ok)
        self.assertIn("size", reason)

    def test_rejects_wide_spread(self) -> None:
        ok, reason, _ = IronCladRiskEngine.validate_order(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=0.5,
            stop_distance=10,
            limit_distance=20,
            bid=50000.0,
            offer=50060.0,
            rest_client=MagicMock(),
        )
        self.assertFalse(ok)
        self.assertIn("slip", reason.lower())

    def test_enforces_stop_limit_floors(self) -> None:
        ok, _, norm = IronCladRiskEngine.validate_order(
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            size=0.5,
            stop_distance=3,
            limit_distance=5,
            bid=1.08,
            offer=1.0802,
            rest_client=MagicMock(),
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(norm["stop_distance"], 10.0)
        self.assertGreaterEqual(norm["limit_distance"], 20.0)

    def test_tick_integrity_nan(self) -> None:
        filt = TickIntegrityFilter()
        q = Quote(
            bid=float("nan"),
            offer=4200.0,
            time=datetime.now(timezone.utc),
        )
        ok, reason = filt.validate_quote(q)
        self.assertFalse(ok)
        self.assertIn("NaN", reason)

    def test_dynamic_threshold_relaxes_low_vol(self) -> None:
        out = dynamic_confidence_floor(
            base_threshold=55.0,
            atr=8.0,
            atr_baseline=20.0,
        )
        self.assertLess(out["adjusted_threshold"], out["base_threshold"])

    def test_post_fill_slippage(self) -> None:
        ok, _ = enforce_slippage_on_fill(
            direction="BUY",
            expected_mid=100.0,
            fill_price=102.0,
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
