"""Tests — per-dealId alpha trail isolation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.alpha_trail import AlphaOptimisedTrailEngine


class AlphaTrailDealTests(unittest.TestCase):
    def test_trails_keyed_by_deal_id(self) -> None:
        engine = AlphaOptimisedTrailEngine()
        position_map = {
            "DEAL-1": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "side": "BUY",
                "entry": 1.16125,
                "stop": 1.16000,
                "size": 1.0,
                "atr": 0.00050,
            },
            "DEAL-2": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "side": "SELL",
                "entry": 1.16200,
                "stop": 1.16300,
                "size": 1.0,
                "atr": 0.00050,
            },
        }
        quotes = {
            "CS.D.EURUSD.CFD.IP": {"bid": 1.16140, "offer": 1.16145},
        }
        trails = engine.compute_for_position_map(
            position_map,
            epic_quotes=quotes,
            micro_verdicts={"CS.D.EURUSD.CFD.IP": {"regime": "NEUTRAL"}},
        )
        self.assertEqual(set(trails.keys()), {"DEAL-1", "DEAL-2"})
        self.assertEqual(trails["DEAL-1"].deal_id, "DEAL-1")
        self.assertEqual(trails["DEAL-2"].deal_id, "DEAL-2")


if __name__ == "__main__":
    unittest.main()
