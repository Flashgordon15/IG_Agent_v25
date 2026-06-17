"""Operational drawdown state + HUD serialization verification."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import system.drawdown_monitor as dm
from system.drawdown_monitor import operational_status, snapshot_for_telemetry
from system.superjet_drawdown_guard import reset_superjet_drawdown_guard_for_tests, telemetry_snapshot


class DrawdownOperationalVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        dm.configure(alert_threshold_pct=999.0)
        dm.reset_session("1000.00", field="balance")
        reset_superjet_drawdown_guard_for_tests()

    def test_operational_status_nominal_after_reset(self) -> None:
        dm.update("1000.00", field="balance")
        self.assertEqual(operational_status(), "NOMINAL")

    def test_operational_status_standby_before_observations(self) -> None:
        dm.reset_session("1000.00", field="balance")
        # reset zeroes observations — first update transitions to NOMINAL
        self.assertEqual(operational_status(), "STANDBY")

    def test_snapshot_for_telemetry_json_safe(self) -> None:
        dm.update("1000.00", field="balance")
        block = snapshot_for_telemetry()
        encoded = json.dumps({"drawdown_guard": {"monitor": block}}, default=str)
        parsed = json.loads(encoded)
        monitor = parsed["drawdown_guard"]["monitor"]
        self.assertEqual(monitor["last_balance_field_used"], "balance")
        self.assertIsInstance(monitor["session_pnl_gbp"], float)
        self.assertEqual(monitor["operational_status"], "NOMINAL")

    def test_superjet_not_breached_when_pnl_nominal(self) -> None:
        snap = telemetry_snapshot()
        self.assertFalse(snap["breached"])
        self.assertFalse(snap["frozen"])


if __name__ == "__main__":
    unittest.main()
