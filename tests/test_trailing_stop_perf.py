"""Performance and isolation tests for trailing stop fast path."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.trailing_stop_engine import TrailEval, eval_trailing_stop
from system import ml_filter_overrides as ml_overrides
from runtime.market_orchestrator import select_active_rotation_epics


class TrailingStopPerfTests(unittest.TestCase):
    def test_eval_trailing_stop_sub_millisecond_batch(self) -> None:
        ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
        iterations = 50_000
        start = time.perf_counter()
        hits = 0
        for i in range(iterations):
            px = 110.0 + (i % 3) * 0.1
            result = eval_trailing_stop(
                TrailEval(ev.side, ev.entry, ev.stop, ev.target, px, ev.profit, ev.trigger, ev.distance)
            )
            if result is not None:
                hits += 1
        elapsed = time.perf_counter() - start
        per_eval_us = (elapsed / iterations) * 1_000_000
        self.assertLess(
            per_eval_us,
            50.0,
            f"trail eval too slow: {per_eval_us:.2f}µs per call",
        )
        self.assertGreater(hits, 0)

    def test_trailing_math_unchanged_for_buy(self) -> None:
        ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
        self.assertAlmostEqual(eval_trailing_stop(ev), 105.0)

    def test_rotation_and_rsi_ramp_unaffected(self) -> None:
        ranked = [
            ("EPIC_A", 100.0),
            ("EPIC_B", 90.0),
            ("EPIC_C", 80.0),
            ("EPIC_D", 76.0),
            ("EPIC_E", 50.0),
        ]
        active = select_active_rotation_epics(ranked)
        self.assertEqual(len(active), 4)
        strict = 16.06
        self.assertEqual(ml_overrides.scale_max_rsi(strict, 50), ml_overrides.BASELINE_MAX_RSI)
        self.assertAlmostEqual(ml_overrides.scale_max_rsi(strict, 500), strict, places=4)


if __name__ == "__main__":
    unittest.main()
