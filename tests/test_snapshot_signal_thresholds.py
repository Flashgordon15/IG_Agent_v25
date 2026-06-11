"""Signal threshold fields on dashboard ticks (WebSocket / state)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.snapshot import enrich_signal_thresholds, normalize_tick
from api.snapshot_store import (
    get_tick,
    publish_tick,
    reset_snapshot_store_for_tests,
    set_snapshot_path_for_tests,
)


class SnapshotSignalThresholdTests(unittest.TestCase):
    def test_enrich_from_gate_value(self) -> None:
        tick = {
            "type": "tick",
            "health": {
                "gates": [
                    {
                        "name": "signal_confidence",
                        "pass": False,
                        "value": {
                            "threshold": 80.0,
                            "config_signal_threshold": 70.0,
                            "min_size_threshold": 88.0,
                            "points_confidence_floor": 80.0,
                            "points_state": "CAUTION",
                            "confidence": 68.0,
                        },
                        "detail": "WAIT",
                    }
                ]
            },
            "signal": {
                "direction": "WAIT",
                "confidence": 68,
                "threshold": 80,
            },
            "points": {"state": "CAUTION"},
        }
        enrich_signal_thresholds(tick)
        sig = tick["signal"]
        self.assertEqual(sig["config_signal_threshold"], 70)
        self.assertEqual(sig["min_size_threshold"], 88)
        self.assertEqual(sig["points_confidence_floor"], 80)
        self.assertEqual(sig["points_state"], "CAUTION")

    def test_enrich_fills_missing_from_points_state(self) -> None:
        tick = {
            "type": "tick",
            "signal": {"direction": "WAIT", "confidence": 68, "threshold": 80},
            "points": {"state": "CAUTION"},
        }
        enrich_signal_thresholds(tick)
        sig = tick["signal"]
        self.assertEqual(sig["min_size_threshold"], 88)
        self.assertEqual(sig["points_confidence_floor"], 55)
        self.assertIsNotNone(sig.get("config_signal_threshold"))

    def test_normalize_tick_always_includes_thresholds(self) -> None:
        tick = normalize_tick(
            {
                "signal": {"direction": "WAIT", "confidence": 0},
                "points": {"state": "HEALTHY"},
            }
        )
        sig = tick["signal"]
        self.assertIn("config_signal_threshold", sig)
        self.assertIn("min_size_threshold", sig)
        self.assertIn("threshold", sig)

    def test_get_tick_enriches_stale_disk_snapshot(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.json"
            reset_snapshot_store_for_tests()
            set_snapshot_path_for_tests(snap)
            publish_tick(
                {
                    "signal": {
                        "direction": "WAIT",
                        "confidence": 68,
                        "threshold": 80,
                    },
                    "points": {"state": "CAUTION"},
                },
                notify=False,
            )
            # Simulate stale on-disk file without threshold fields
            import json

            raw = json.loads(snap.read_text(encoding="utf-8"))
            raw["signal"] = {
                "direction": "WAIT",
                "confidence": 68,
                "threshold": 80,
            }
            snap.write_text(json.dumps(raw), encoding="utf-8")
            reset_snapshot_store_for_tests()
            set_snapshot_path_for_tests(snap)
            sig = get_tick()["signal"]
            self.assertEqual(sig["config_signal_threshold"], 70)
            self.assertEqual(sig["min_size_threshold"], 88)


if __name__ == "__main__":
    unittest.main()
