"""Superjet HUD backend modules — drift, triage, drawdown, rocket trigger."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.position_drift import build_position_drift_report, detect_deal_drift
from execution.rocket_trigger import rocket_trigger_eligible
from system.superjet_drawdown_guard import (
    MAX_DAILY_DRAWDOWN_GBP,
    reset_superjet_drawdown_guard_for_tests,
    telemetry_snapshot,
)
from system.supervisor_history import (
    filter_superseded_triage_events,
    read_history_last_24h,
    record_supervisor_event,
    reset_supervisor_history_for_tests,
)


class SuperjetHudTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_supervisor_history_for_tests()
        reset_superjet_drawdown_guard_for_tests()

    def test_drift_detects_size_mismatch(self) -> None:
        local = {"epic": "CS.D.EURUSD.CFD.IP", "size": 2.0, "entry": 1.085}
        broker = {"epic": "CS.D.EURUSD.CFD.IP", "size": 1.0, "entry": 1.085}
        hit = detect_deal_drift("D1", local, broker)
        self.assertIsNotNone(hit)
        self.assertTrue(hit["drift_detected"])

    def test_drift_report_clean_when_aligned(self) -> None:
        row = {"epic": "IX.D.DOW.IFM.IP", "size": 1.0, "entry": 43300.0}
        report = build_position_drift_report(
            broker_map={"D1": dict(row)},
            local_map={"D1": dict(row)},
        )
        self.assertFalse(report["any_drift"])

    def test_supervisor_history_roundtrip(self) -> None:
        record_supervisor_event("port_flush", detail="test")
        rows = read_history_last_24h()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[-1]["event_type"], "port_flush")

    def test_triage_filters_superseded_drawdown_breach_when_pnl_reset(self) -> None:
        record_supervisor_event(
            "drawdown_ceiling_breach",
            detail="P&L -500.00 GBP",
            source="superjet_drawdown_guard",
        )
        record_supervisor_event("port_flush", detail="ok")
        rows = [
            {"event_type": "drawdown_ceiling_breach", "detail": "P&L -500.00 GBP"},
            {"event_type": "port_flush", "detail": "ok"},
        ]
        with patch(
            "system.supervisor_history._historic_drawdown_breach_superseded",
            return_value=(True, "effective_pnl_zero_monitor_nominal"),
        ):
            filtered = filter_superseded_triage_events(rows)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["event_type"], "port_flush")
        with patch(
            "system.supervisor_history._historic_drawdown_breach_superseded",
            return_value=(False, "superjet_active"),
        ):
            unfiltered = filter_superseded_triage_events(rows)
        self.assertEqual(len(unfiltered), 2)

    def test_drawdown_guard_ceiling_constant(self) -> None:
        self.assertEqual(MAX_DAILY_DRAWDOWN_GBP, 500.0)

    def test_drawdown_telemetry_shape(self) -> None:
        with patch(
            "system.superjet_drawdown_guard._resolve_daily_pnl_gbp",
            return_value=-120.0,
        ):
            snap = telemetry_snapshot()
        self.assertIn("daily_pnl_gbp", snap)
        self.assertFalse(snap["frozen"])

    def test_rocket_trigger_nominal_without_micro(self) -> None:
        with patch(
            "execution.rocket_trigger.rocket_trigger_eligible",
            wraps=rocket_trigger_eligible,
        ):
            ok, reason = rocket_trigger_eligible("")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
