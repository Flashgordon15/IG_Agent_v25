"""Probe/core/full risk band sizing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.risk_bands import (
    apply_risk_band_to_size,
    probe_risk_target_gbp,
    reset_risk_bands_cache_for_tests,
    risk_band_for_confidence,
    threshold_pass_map,
)


class RiskBandTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_risk_bands_cache_for_tests()

    def test_probe_band_72_to_80(self) -> None:
        self.assertEqual(risk_band_for_confidence(71.0), "below_floor")
        self.assertEqual(risk_band_for_confidence(75.0), "probe")
        self.assertEqual(risk_band_for_confidence(79.0), "probe")
        self.assertEqual(risk_band_for_confidence(82.0), "core")
        self.assertEqual(risk_band_for_confidence(88.0), "full")

    def test_probe_risk_interpolates(self) -> None:
        lo = probe_risk_target_gbp(72.0)
        mid = probe_risk_target_gbp(76.0)
        hi = probe_risk_target_gbp(80.0)
        self.assertAlmostEqual(lo, 50.0, delta=1.0)
        self.assertGreater(mid, lo)
        self.assertLess(mid, hi)
        self.assertAlmostEqual(hi, 80.0, delta=1.0)

    def test_probe_clips_size(self) -> None:
        # DOW-like: 80 stop × 7.87 £/pt — full size 0.3 → ~£189 risk
        size, band, note = apply_risk_band_to_size(
            0.3,
            confidence=76.0,
            stop_pts=80.0,
            point_value_gbp=7.87,
            epic_risk_cap_gbp=150.0,
        )
        self.assertEqual(band, "probe")
        self.assertIn("probe", note)
        risk = 80.0 * size * 7.87
        self.assertLessEqual(risk, 85.0)

    def test_threshold_pass_map(self) -> None:
        m = threshold_pass_map(78.0, "BUY")
        self.assertTrue(m[">=70"])
        self.assertTrue(m[">=75"])
        self.assertFalse(m[">=80"])
        self.assertFalse(m[">=85"])


if __name__ == "__main__":
    unittest.main()
