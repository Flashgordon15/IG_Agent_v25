"""Tests for trading.ml_scorer — score() contract, never raises."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.ml_scorer import MLScorer


class MLScorerTests(unittest.TestCase):
    def test_zero_when_disabled(self) -> None:
        scorer = MLScorer()
        self.assertEqual(
            scorer.score({"confidence": 80.0}, use_ml_signal=False),
            0.0,
        )

    def test_zero_when_no_model(self) -> None:
        scorer = MLScorer()
        scorer._model = None
        self.assertEqual(
            scorer.score({"confidence": 80.0}, use_ml_signal=True),
            0.0,
        )

    def test_zero_on_timeout(self) -> None:
        scorer = MLScorer()
        scorer._model = object()
        scorer._feature_names = ["confidence"]

        def slow_predict(_features: dict[str, float]) -> float:
            time.sleep(0.2)
            return 0.9

        scorer.predict = slow_predict  # type: ignore[method-assign]
        self.assertEqual(
            scorer.score({"confidence": 80.0}, use_ml_signal=True, timeout_s=0.01),
            0.0,
        )

    def test_never_raises(self) -> None:
        scorer = MLScorer()
        scorer._model = MagicMock()
        scorer._feature_names = ["confidence"]
        with patch.object(scorer, "predict", side_effect=RuntimeError("boom")):
            self.assertEqual(
                scorer.score({"confidence": 1.0}, use_ml_signal=True),
                0.0,
            )
        self.assertEqual(
            scorer.score(None, use_ml_signal=False),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
