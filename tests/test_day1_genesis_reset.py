"""Day 1 Genesis Reset Protocol — unit tests."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.day1_genesis_reset import (
    FRESH_POINTS_STATE,
    genesis_reset_enabled,
    reset_day1_genesis_for_tests,
    run_day1_genesis_reset,
)


class Day1GenesisResetTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_day1_genesis_for_tests()
        os.environ.pop("DAY1_GENESIS", None)

    def test_skipped_without_env_flag(self) -> None:
        os.environ.pop("DAY1_GENESIS", None)
        result = run_day1_genesis_reset(force=False)
        self.assertTrue(result.get("skipped"))

    def test_force_writes_fresh_points_state(self) -> None:
        with patch("system.day1_genesis_reset._collect_purge_paths", return_value=[]):
            with patch("system.day1_genesis_reset._zero_ledger_and_metrics") as mock_zero:
                mock_zero.return_value = {"effective_daily_pnl_gbp": 0.0}
                manifest = run_day1_genesis_reset(force=True)
        self.assertEqual(manifest.get("event"), "DAY1_GENESIS_RESET")
        points_path = ROOT / "src" / "data" / "state" / "points_state.json"
        self.assertTrue(points_path.is_file())
        data = json.loads(points_path.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], FRESH_POINTS_STATE["state"])
        self.assertEqual(data["consecutive_losses"], 0)

    def test_genesis_env_flag_detection(self) -> None:
        os.environ["DAY1_GENESIS"] = "1"
        self.assertTrue(genesis_reset_enabled())


if __name__ == "__main__":
    unittest.main()
