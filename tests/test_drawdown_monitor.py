"""Tests for peak-equity drawdown monitor."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import system.drawdown_monitor as dm


class DrawdownMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Suppress alert threshold so no Telegram thread fires during tests
        dm.configure(alert_threshold_pct=999.0)
        dm.reset_session(10_000.0)

    def test_zero_drawdown_at_start(self) -> None:
        snap = dm.snapshot()
        self.assertEqual(snap["drawdown_gbp"], 0.0)
        self.assertEqual(snap["drawdown_pct"], 0.0)

    def test_calculates_drawdown_correctly(self) -> None:
        dm.update(9_500.0)
        snap = dm.snapshot()
        self.assertAlmostEqual(snap["drawdown_gbp"], 500.0, places=1)
        self.assertAlmostEqual(snap["drawdown_pct"], 5.0, places=1)

    def test_peak_advances_on_new_high(self) -> None:
        dm.update(11_000.0)
        snap = dm.snapshot()
        self.assertAlmostEqual(snap["peak_balance"], 11_000.0, places=1)
        self.assertAlmostEqual(snap["drawdown_gbp"], 0.0, places=1)

    def test_max_drawdown_tracks_worst_point(self) -> None:
        dm.update(9_000.0)   # 10% drawdown
        dm.update(9_500.0)   # recovery
        snap = dm.snapshot()
        self.assertAlmostEqual(snap["max_drawdown_gbp"], 1_000.0, places=1)
        self.assertAlmostEqual(snap["max_drawdown_pct"], 10.0, places=1)

    def test_session_pnl(self) -> None:
        dm.update(10_200.0)
        snap = dm.snapshot()
        self.assertAlmostEqual(snap["session_pnl_gbp"], 200.0, places=1)

    def test_reset_clears_history(self) -> None:
        dm.update(8_000.0)
        dm.reset_session(12_000.0)
        snap = dm.snapshot()
        self.assertAlmostEqual(snap["peak_balance"], 12_000.0, places=1)
        self.assertAlmostEqual(snap["max_drawdown_gbp"], 0.0, places=1)


if __name__ == "__main__":
    unittest.main()
