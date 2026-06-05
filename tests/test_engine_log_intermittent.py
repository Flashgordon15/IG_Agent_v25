"""Tests for intermittent engine logging."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system import engine_log


class EngineLogIntermittentTests(unittest.TestCase):
    def setUp(self) -> None:
        engine_log.reset_intermittent_log_state_for_tests()
        engine_log._intermittent_enabled = True
        engine_log._intermittent_interval_sec = 60.0

    def tearDown(self) -> None:
        engine_log.reset_intermittent_log_state_for_tests()

    @patch("system.engine_log.log_engine")
    def test_throttles_repeated_key(self, mock_log) -> None:
        self.assertTrue(
            engine_log.log_engine_intermittent("k1", "first", interval_sec=60.0)
        )
        self.assertFalse(
            engine_log.log_engine_intermittent("k1", "second", interval_sec=60.0)
        )
        self.assertEqual(mock_log.call_count, 1)

    @patch("system.engine_log.log_engine")
    def test_force_always_logs(self, mock_log) -> None:
        engine_log.log_engine_intermittent("k1", "a", interval_sec=60.0)
        engine_log.log_engine_intermittent("k1", "b", interval_sec=60.0, force=True)
        self.assertEqual(mock_log.call_count, 2)


if __name__ == "__main__":
    unittest.main()
