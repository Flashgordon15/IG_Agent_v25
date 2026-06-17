"""Scalping telemetry — Flight Deck Card C authoritative metrics."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.scalping_telemetry import collect_scalping_telemetry
from intelligence.microstructure import MicrostructureClassifier
from intelligence.time_decay import (
    max_stall_seconds,
    reset_time_decay_state_for_tests,
    scalping_time_decay_telemetry,
)
from intelligence.velocity_filter import scalping_velocity_telemetry


class ScalpingTelemetryTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_time_decay_state_for_tests()

    def test_time_decay_inactive_below_activation(self) -> None:
        snap = scalping_time_decay_telemetry({})
        self.assertFalse(snap["active"])
        self.assertEqual(snap["atr_compress_pct"], 0)
        self.assertEqual(snap["compression_ratio"], 0.0)

    def test_time_decay_compresses_after_45s_stall(self) -> None:
        pmap = {"D1": {"open_mins": 55.0 / 60.0}}
        snap = scalping_time_decay_telemetry(pmap)
        self.assertTrue(snap["active"])
        self.assertEqual(snap["atr_compress_pct"], 30)
        self.assertEqual(snap["interval_sec"], 10)
        self.assertEqual(snap["step_pct"], 15)

    def test_time_decay_caps_at_75_percent(self) -> None:
        pmap = {"D1": {"stall_seconds": 200.0}}
        snap = scalping_time_decay_telemetry(pmap)
        self.assertEqual(snap["atr_compress_pct"], 75)
        self.assertEqual(snap["compression_ratio"], 0.75)

    def test_tick_velocity_override_on_burst(self) -> None:
        snap = scalping_velocity_telemetry("IX.D.NASDAQ.IFM.IP", micro_confidence=0.5)
        with patch("intelligence.velocity_filter.ticks_in_window", return_value=16):
            snap = scalping_velocity_telemetry("IX.D.NASDAQ.IFM.IP", micro_confidence=0.5)
        self.assertTrue(snap["override_active"])
        self.assertEqual(snap["ticks_200ms"], 16)

    def test_tick_velocity_override_on_confidence(self) -> None:
        with patch("intelligence.velocity_filter.ticks_in_window", return_value=3):
            snap = scalping_velocity_telemetry("IX.D.NASDAQ.IFM.IP", micro_confidence=0.92)
        self.assertTrue(snap["override_active"])
        self.assertEqual(snap["confidence_pct"], 92.0)

    def test_max_stall_from_open_mins(self) -> None:
        pmap = {
            "A": {"open_mins": 0.5},
            "B": {"open_mins": 1.2},
        }
        self.assertEqual(max_stall_seconds(pmap), 72.0)

    def test_microstructure_ticks_in_window(self) -> None:
        clf = MicrostructureClassifier()
        epic = "IX.D.NASDAQ.IFM.IP"
        t0 = time.time()
        for i in range(20):
            clf.record_tick(
                epic,
                bid=100.0 + i * 0.01,
                offer=100.5 + i * 0.01,
                ts=t0 - 0.15 + i * 0.01,
            )
        recent = clf.ticks_in_window(epic, 0.2, now=t0)
        self.assertGreaterEqual(recent, 15)

    def test_collect_scalping_telemetry_engaged(self) -> None:
        pmap = {"D1": {"open_mins": 1.0, "epic": "IX.D.NASDAQ.IFM.IP"}}
        with patch("intelligence.velocity_filter.ticks_in_window", return_value=18):
            out = collect_scalping_telemetry(
                position_map=pmap,
                primary_epic="IX.D.NASDAQ.IFM.IP",
                micro_confidence=0.88,
            )
        self.assertEqual(out["engine_state"], "ENGAGED")
        self.assertTrue(out["tick_velocity"]["override_active"])
        self.assertEqual(out["open_positions"], 1)

    def test_telemetry_snapshot_includes_scalping_block(self) -> None:
        from cockpit.telemetry_bridge import _collect_snapshot

        fake_state = MagicMock()
        fake_state.snapshot_model.return_value.to_dict.return_value = {
            "gates": {"G1": {"status": "complete"}},
            "phase": "READY",
        }
        with patch("system.system_state.get_system_state", return_value=fake_state):
            with patch("api.agent_control.get_trading_loop", return_value=None):
                with patch("intelligence.velocity_filter.ticks_in_window", return_value=4):
                    payload = _collect_snapshot(("IX.D.NASDAQ.IFM.IP",))
        self.assertIn("scalping_telemetry", payload)
        st = payload["scalping_telemetry"]
        self.assertEqual(st["engine_state"], "STANDBY")
        self.assertIn("time_decay", st)
        self.assertIn("tick_velocity", st)
        self.assertIn("compression_ratio", st["time_decay"])


if __name__ == "__main__":
    unittest.main()
