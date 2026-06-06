"""Tests for portfolio-level correlation guard."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.correlation_guard import (
    _STATE_FILE,
    MAX_NEW_PER_DIRECTION,
    check_and_record,
    reset_correlation_guard_for_tests,
    reset_session,
    snapshot,
    undo,
)


class CorrelationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_correlation_guard_for_tests()
        reset_session(key="test-session")

    def test_allows_first_entry(self) -> None:
        ok, reason = check_and_record("BUY")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_counts_separately_per_direction(self) -> None:
        for _ in range(3):
            check_and_record("BUY")
        for _ in range(3):
            check_and_record("SELL")
        snap = snapshot()
        self.assertEqual(snap["buy"], 3)
        self.assertEqual(snap["sell"], 3)

    def test_blocks_at_max(self) -> None:
        for _ in range(MAX_NEW_PER_DIRECTION):
            ok, _ = check_and_record("BUY")
            self.assertTrue(ok)
        ok, reason = check_and_record("BUY")
        self.assertFalse(ok)
        self.assertIn("BUY", reason)
        self.assertIn(str(MAX_NEW_PER_DIRECTION), reason)

    def test_sell_not_blocked_when_buy_full(self) -> None:
        for _ in range(MAX_NEW_PER_DIRECTION):
            check_and_record("BUY")
        ok, _ = check_and_record("SELL")
        self.assertTrue(ok)

    def test_undo_releases_slot(self) -> None:
        for _ in range(MAX_NEW_PER_DIRECTION):
            check_and_record("BUY")
        undo("BUY")
        ok, _ = check_and_record("BUY")
        self.assertTrue(ok)

    def test_unknown_direction_always_allowed(self) -> None:
        ok, _ = check_and_record("WAIT")
        self.assertTrue(ok)

    def test_reset_clears_counts(self) -> None:
        for _ in range(MAX_NEW_PER_DIRECTION):
            check_and_record("BUY")
        reset_session(key="new-session")
        ok, _ = check_and_record("BUY")
        self.assertTrue(ok)
        snap = snapshot()
        self.assertEqual(snap["buy"], 1)

    def test_persists_counts_to_disk(self) -> None:
        check_and_record("BUY")
        check_and_record("SELL")
        self.assertTrue(_STATE_FILE.is_file())
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(raw["buy"], 1)
        self.assertEqual(raw["sell"], 1)
        self.assertEqual(raw["session"], "test-session")


if __name__ == "__main__":
    unittest.main()
