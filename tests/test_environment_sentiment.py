"""Tests for IG client sentiment in environment fitness."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from trading.environment_scorer import EnvironmentScorer


class EnvironmentSentimentTests(unittest.TestCase):
    def test_crowded_long_reduces_score(self) -> None:
        rest = MagicMock()
        rest.fetch_client_sentiment.return_value = 85.0
        scorer = EnvironmentScorer(rest_client=rest, epic="IX.D.NIKKEI.IFM.IP")
        scorer.fetch_sentiment("IX.D.NIKKEI.IFM.IP")
        detail = scorer.get_sentiment_factor("japan_225")
        self.assertEqual(detail["label"], "crowded_long")
        self.assertEqual(detail["adjustment"], -10.0)

    def test_sentiment_error_is_neutral(self) -> None:
        rest = MagicMock()
        rest.fetch_client_sentiment.side_effect = RuntimeError("fail")
        scorer = EnvironmentScorer(rest_client=rest, epic="EPIC")
        pct = scorer.fetch_sentiment("EPIC")
        self.assertEqual(pct, 50.0)


if __name__ == "__main__":
    unittest.main()
