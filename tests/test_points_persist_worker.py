"""Tests for background points state persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading.points_engine import (
    PointsEngine,
    flush_points_persist,
    reset_points_persist_for_tests,
    set_points_state_path_for_tests,
)


class PointsPersistWorkerTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_points_persist_for_tests()
        set_points_state_path_for_tests(None)

    def test_consume_signal_skip_does_not_write_until_flush(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state_path = Path(tmp.name) / "points.json"
        set_points_state_path_for_tests(state_path)
        engine = PointsEngine(state_path=state_path)
        engine._signals_to_skip = 1
        engine.consume_signal_skip()
        self.assertFalse(state_path.exists())
        flush_points_persist()
        self.assertTrue(state_path.exists())
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["signals_to_skip"], 0)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
