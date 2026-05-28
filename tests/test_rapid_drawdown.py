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

    def test_forces_warning_after_100_gbp_loss_in_hour(self) -> None:
        self.engine.note_realised_gbp_loss(-60.0)
        self.engine.note_realised_gbp_loss(-50.0)
        self.assertEqual(self.engine.get_state(), "WARNING")


if __name__ == "__main__":
    unittest.main()
