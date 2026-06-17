"""Unit tests — IntelligenceComputeWorker non-blocking ingress."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.intelligence_worker import IntelligenceComputeWorker
from intelligence.pipeline_bridge import IntelligenceLayer, reset_intelligence_layer_for_tests


class IntelligenceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_intelligence_layer_for_tests()
        self.worker = IntelligenceComputeWorker(interval_sec=0.05)

    def tearDown(self) -> None:
        self.worker.stop()
        reset_intelligence_layer_for_tests()

    def test_enqueue_is_non_blocking(self) -> None:
        t0 = time.perf_counter()
        for i in range(500):
            self.worker.enqueue_tick(
                "IX.D.NASDAQ.IFM.IP",
                bid=18000.0 + i * 0.1,
                offer=18000.5 + i * 0.1,
                ts=time.time(),
            )
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.5)

    def test_tick_once_populates_snapshot(self) -> None:
        epic = "CS.D.CFPGOLD.CFP.IP"
        for i in range(30):
            spread = 0.4 + (i % 5) * 0.02
            self.worker.enqueue_tick(
                epic,
                bid=2350.0,
                offer=2350.0 + spread,
                ts=time.time(),
            )
        updated = self.worker.tick_once()
        self.assertGreaterEqual(updated, 1)
        snap = self.worker.get_snapshot()
        self.assertIn(epic, snap.spread)
        self.assertIn(epic, snap.microstructure)

    def test_pipeline_bridge_reads_verdicts(self) -> None:
        layer = IntelligenceLayer(worker=self.worker)
        epic = "IX.D.DOW.IFM.IP"
        for i in range(25):
            self.worker.enqueue_tick(
                epic,
                bid=39000.0 + i,
                offer=39000.5 + i,
            )
        self.worker.tick_once()
        adj = layer.execution_adjustments(epic)
        self.assertIn("intelligence_throttle_factor", adj)
        self.assertIn("intelligence_micro_regime", adj)

    def test_on_hub_tick_flushes_and_recomputes(self) -> None:
        layer = IntelligenceLayer(worker=self.worker)
        epic = "IX.D.NASDAQ.IFM.IP"
        layer.on_hub_tick(epic, bid=18000.0, offer=18000.5, ts=time.time())
        snap = self.worker.get_snapshot()
        self.assertIn(epic, snap.microstructure)
        v1 = layer.microstructure_verdict(epic)
        layer.on_hub_tick(epic, bid=18050.0, offer=18050.5, ts=time.time())
        v2 = layer.microstructure_verdict(epic)
        self.assertGreaterEqual(v2.confidence, 0.35)
        self.assertIsNotNone(v1.detail)


if __name__ == "__main__":
    unittest.main()
