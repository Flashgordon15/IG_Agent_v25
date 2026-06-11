from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading.points_engine import PointsEngine, set_points_state_path_for_tests


class RapidDrawdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self.tmp.name) / "points.json")
        self.engine = PointsEngine()

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_forces_warning_after_rapid_drawdown_threshold(self) -> None:
        # RAPID_DRAWDOWN_GBP = 2000; cumulative must exceed that in one hour
        from trading.points_engine import RAPID_DRAWDOWN_GBP
        loss_per_event = (RAPID_DRAWDOWN_GBP / 5) + 1.0
        for _ in range(6):
            self.engine.note_realised_gbp_loss(-loss_per_event)
        self.assertEqual(self.engine.get_state(), "WARNING")


if __name__ == "__main__":
    unittest.main()
