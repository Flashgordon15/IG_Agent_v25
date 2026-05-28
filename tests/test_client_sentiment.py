from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from trading.environment_scorer import EnvironmentScorer


class ClientSentimentTests(unittest.TestCase):
    def test_crowded_long_penalty(self) -> None:
        rest = MagicMock()
        rest.fetch_client_sentiment.return_value = 85.0
        scorer = EnvironmentScorer(None, rest_client=rest, epic="IX.D.NIKKEI.IFM.IP")
        scorer.fetch_sentiment("IX.D.NIKKEI.IFM.IP")
        factor = scorer.get_sentiment_factor("Japan 225")
        self.assertEqual(factor["label"], "crowded_long")
        self.assertEqual(factor["adjustment"], -10.0)


if __name__ == "__main__":
    unittest.main()
