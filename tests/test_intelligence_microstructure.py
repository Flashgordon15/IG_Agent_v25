"""Unit tests — Multi-Timeframe Micro-Structure Classifier (mock ticks)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from datetime import datetime, timedelta
from intelligence.microstructure import (
    MIN_WARMUP_TICKS,
    MicrostructureClassifier,
    bootstrap_microstructure_for_loop,
)


def _feed_trend(
    clf: MicrostructureClassifier,
    epic: str,
    *,
    start: float,
    step: float,
    n: int,
    t0: float,
    dt: float = 0.2,
) -> None:
    mid = start
    for i in range(n):
        mid += step
        spread = 0.5
        bid = mid - spread / 2
        offer = mid + spread / 2
        clf.record_tick(epic, bid=bid, offer=offer, ts=t0 + i * dt)


class MicrostructureClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clf = MicrostructureClassifier(sweep_sigma=2.0, min_ticks_5s=4)

    def test_uptrend_momentum_classification(self) -> None:
        epic = "IX.D.NASDAQ.IFM.IP"
        t0 = time.time() - 10.0
        _feed_trend(self.clf, epic, start=18000.0, step=2.5, n=40, t0=t0)
        v = self.clf.classify(epic, now=t0 + 8.0)
        self.assertIn(v.regime, ("MOMENTUM_UP", "SWEEP_BUY", "NEUTRAL"))
        self.assertGreaterEqual(v.confidence, 0.3)

    def test_sweep_detection_on_impulse(self) -> None:
        epic = "IX.D.DOW.IFM.IP"
        t0 = time.time() - 5.0
        mid = 39000.0
        for i in range(8):
            self.clf.record_tick(
                epic,
                bid=mid - 0.25,
                offer=mid + 0.25,
                ts=t0 + i * 0.3,
            )
        for jump in range(6):
            mid += 25.0
            self.clf.record_tick(
                epic,
                bid=mid - 0.25,
                offer=mid + 0.25,
                ts=t0 + 3.0 + jump * 0.15,
            )
        v = self.clf.classify(epic, now=t0 + 5.0)
        self.assertTrue(
            v.sweep_detected or v.regime in ("SWEEP_BUY", "MOMENTUM_UP"),
            f"expected sweep or momentum, got {v.regime}",
        )

    def test_neutral_on_flat_noise(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        t0 = time.time() - 3.0
        for i in range(20):
            self.clf.record_tick(
                epic,
                bid=1.08496,
                offer=1.08504,
                ts=t0 + i * 0.25,
            )
        v = self.clf.classify(epic, now=t0 + 5.0)
        self.assertEqual(v.regime, "NEUTRAL")
        self.assertLess(v.confidence, 0.5)

    def test_seed_from_quotes_warms_empty_buffer(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        self.assertTrue(self.clf.needs_historical_warmup(epic))
        t0 = datetime.now() - timedelta(hours=2)
        quotes = []
        mid = 1.0850
        for i in range(100):
            quotes.append(
                Quote(
                    time=t0 + timedelta(minutes=5 * i),
                    bid=mid + i * 0.00001,
                    offer=mid + i * 0.00001 + 0.00008,
                )
            )
        seeded = self.clf.seed_from_quotes(epic, quotes)
        self.assertGreaterEqual(seeded, MIN_WARMUP_TICKS)
        self.assertFalse(self.clf.needs_historical_warmup(epic))
        v = self.clf.classify(epic)
        self.assertGreater(v.confidence, 0.35)

    def test_bootstrap_loop_uses_signal_engine_seed(self) -> None:
        epic = "CS.D.CFPGOLD.CFP.IP"
        loop = MagicMock()
        loop._epic = epic
        loop._market = "Gold"
        quotes = [
            Quote(
                time=datetime.now() - timedelta(minutes=5 * i),
                bid=2350.0 + i,
                offer=2350.5 + i,
            )
            for i in range(80)
        ]
        loop._signal_engine.quote_df.return_value = __import__("pandas").DataFrame(
            [
                {
                    "time": q.time,
                    "bid": q.bid,
                    "offer": q.offer,
                    "mid": (q.bid + q.offer) / 2,
                    "spread": q.offer - q.bid,
                }
                for q in quotes
            ]
        )
        count = bootstrap_microstructure_for_loop(loop, None, classifier=self.clf)
        self.assertGreaterEqual(count, MIN_WARMUP_TICKS)


if __name__ == "__main__":
    unittest.main()
