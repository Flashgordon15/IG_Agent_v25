"""Execution protect boundary tests."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.execution_protect import (
    check_local_spread,
    current_spread,
    is_protect_enabled,
    micro_breakeven_trigger,
    protect_settings,
)
from execution.scalping.dynamic_spread_filter import DynamicSpreadFilter


class ExecutionProtectTests(unittest.TestCase):
    def test_spread_abort_above_ma(self) -> None:
        filt = DynamicSpreadFilter(periods=20, multiplier=1.5, min_samples=3)
        for _ in range(5):
            filt.record("EPIC", 2.0)
        ok, _ = filt.allows("EPIC", 2.0)
        self.assertTrue(ok)
        ok, msg = filt.allows("EPIC", 5.0)
        self.assertFalse(ok)
        self.assertIn("Spread filter", msg)

    def test_current_spread_bid_ask(self) -> None:
        self.assertAlmostEqual(current_spread(100.0, 102.5), 2.5)

    def test_micro_breakeven_math(self) -> None:
        q = Quote(time=datetime.now(timezone.utc), bid=100.0, offer=102.0)
        cfg = {
            "execution_protect": {
                "commission_points_per_side": 0.5,
                "breakeven_buffer_points": 2.0,
            }
        }
        self.assertAlmostEqual(micro_breakeven_trigger(q, cfg), 5.0)

    def test_protect_enabled_from_config(self) -> None:
        cfg = {"execution_protect": {"enabled": True}}
        self.assertTrue(is_protect_enabled(cfg))


if __name__ == "__main__":
    unittest.main()
