"""Tests for live calendar gate."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.calendar_gate import (
    is_calendar_blocked,
    reset_calendar_gate_cache_for_tests,
)


class CalendarGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_calendar_gate_cache_for_tests()

    def test_blocks_during_finnhub_event_window(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        at = datetime(2026, 6, 9, 0, 25, tzinfo=timezone.utc)
        blocked, reason = is_calendar_blocked(epic, at=at)
        self.assertTrue(blocked)
        self.assertIn("calendar", reason.lower())

    def test_passes_outside_window(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        at = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
        blocked, _ = is_calendar_blocked(epic, at=at)
        self.assertFalse(blocked)


class CalendarOkGateTests(unittest.TestCase):
    def test_gate_off_when_disabled(self) -> None:
        from trading.trading_loop import TradingLoop

        loop = TradingLoop.__new__(TradingLoop)
        loop._epic = "CS.D.EURUSD.CFD.IP"
        with patch(
            "system.v26_config.calendar_settings",
            return_value={"enabled": False},
        ):
            gate = loop._gate_calendar_ok()
        self.assertTrue(gate.passed)
        self.assertEqual(gate.value, "off")


if __name__ == "__main__":
    unittest.main()
