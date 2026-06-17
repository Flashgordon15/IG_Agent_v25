"""Chaos-hardening audit tests — defensive defaults and queue guards."""

from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.queue_guard import put_drop_oldest
from intelligence.defensive_defaults import (
    neutral_microstructure_verdict,
    neutral_spread_verdict,
)
from intelligence.microstructure import MicrostructureClassifier
from intelligence.spread_forecast import SpreadWideningForecast
from system.network_bounds import MAX_LOOP_SAFE_TIMEOUT_SEC, clamp_read_timeout


class AuditHardeningTests(unittest.TestCase):
    def test_neutral_defaults_on_empty_epic(self) -> None:
        micro = neutral_microstructure_verdict()
        self.assertEqual(micro.regime, "NEUTRAL")
        spread = neutral_spread_verdict()
        self.assertFalse(spread.blocked)
        self.assertEqual(spread.z_score, 0.0)

    def test_microstructure_empty_buffer_neutral(self) -> None:
        clf = MicrostructureClassifier()
        v = clf.classify("IX.D.DOW.IFM.IP")
        self.assertEqual(v.regime, "NEUTRAL")
        self.assertEqual(v.confidence, 0.35)

    def test_spread_forecast_cold_cache_neutral(self) -> None:
        model = SpreadWideningForecast()
        v = model.compute("CS.D.EURUSD.CFD.IP")
        self.assertFalse(v.blocked)
        self.assertEqual(v.z_score, 0.0)

    def test_put_drop_oldest_never_blocks(self) -> None:
        q: queue.Queue[int] = queue.Queue(maxsize=2)
        for i in range(100):
            put_drop_oldest(q, i)
        self.assertEqual(q.qsize(), 2)
        self.assertEqual(q.get_nowait(), 98)
        self.assertEqual(q.get_nowait(), 99)

    def test_read_timeout_capped_at_five_seconds(self) -> None:
        self.assertEqual(clamp_read_timeout("GET", 45.0, default=45.0), 5.0)
        self.assertEqual(clamp_read_timeout("POST", 45.0, default=45.0), 45.0)
        self.assertEqual(MAX_LOOP_SAFE_TIMEOUT_SEC, 5.0)


if __name__ == "__main__":
    unittest.main()
