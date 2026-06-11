from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading.points_engine import PointsEngine, set_points_state_path_for_tests


class PointsStateKeysTests(unittest.TestCase):
    def test_payload_uses_state_and_cumulative_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "points_state.json"
            set_points_state_path_for_tests(path)
            engine = PointsEngine()
            engine.record_trade("WIN", 90.0, 5.0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("state", data)
            self.assertIn("cumulative_points", data)
            self.assertEqual(data["cumulative_points"], data["cumulative"])
            set_points_state_path_for_tests(None)


if __name__ == "__main__":
    unittest.main()
