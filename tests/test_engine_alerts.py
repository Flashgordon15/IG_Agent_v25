"""Dashboard engine warning alerts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.engine_log import (
    get_engine_alerts_snapshot,
    record_engine_warning,
    reset_engine_alerts_for_tests,
)
from trading.environment_scorer import SAFE_DEFAULT_SCORE, EnvironmentScorer


class EngineAlertsTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_alerts_for_tests()

    def test_record_engine_warning_increments_snapshot(self) -> None:
        reset_engine_alerts_for_tests()
        record_engine_warning("test_kind", "hello")
        snap = get_engine_alerts_snapshot()
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["type"], "test_kind")

    def test_env_scorer_fallback_records_warning(self) -> None:
        reset_engine_alerts_for_tests()
        scorer = EnvironmentScorer(None)
        with patch.object(
            EnvironmentScorer, "_compute_factors", side_effect=RuntimeError("boom")
        ):
            total = scorer.score("Japan 225")
        self.assertEqual(total, SAFE_DEFAULT_SCORE)
        snap = get_engine_alerts_snapshot()
        self.assertGreaterEqual(snap["count"], 1)
        self.assertEqual(snap["type"], "env_scorer_fallback")

    def test_env_scorer_insufficient_bars_warmup_not_counted(self) -> None:
        reset_engine_alerts_for_tests()
        scorer = EnvironmentScorer(None)
        with patch.object(
            EnvironmentScorer, "_compute_factors", side_effect=ValueError("insufficient bars")
        ):
            total = scorer.score("Japan 225")
        self.assertEqual(total, SAFE_DEFAULT_SCORE)
        snap = get_engine_alerts_snapshot()
        self.assertEqual(snap["count"], 0)


if __name__ == "__main__":
    unittest.main()
