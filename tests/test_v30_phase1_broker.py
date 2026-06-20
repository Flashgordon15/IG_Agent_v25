"""
Phase 1 — Multi-API ingestion broker verification (Worker A isolation).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote

_EPIC = "CS.D.CFPGOLD.CFP.IP"
_FLOOD = 2000


def _quote(bid: float, offer: float | None = None) -> Quote:
    off = offer if offer is not None else bid + 0.4
    return Quote(datetime(2026, 6, 19, 14, 0), bid, off)


class V30Phase1BrokerTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("IG_MULTI_API_BROKER", None)
        os.environ.pop("NODE_ENV", None)
        from apex.microkernel import reset_microkernel_for_tests
        from apex.warmup_progress import reset_warmup_for_tests
        from system.system_state import SystemState
        from trading.multi_api_broker import reset_multi_api_broker_for_tests

        reset_multi_api_broker_for_tests()
        reset_microkernel_for_tests()
        reset_warmup_for_tests()
        SystemState.reset_singleton_for_tests()

    def test_worker_a_warmup_isolation_broker_flush_and_math_sla(self) -> None:
        os.environ["NODE_ENV"] = "production"
        os.environ["IG_MULTI_API_BROKER"] = "0"

        from apex.microkernel import (
            DEFERRED_FLUSH_CHUNK,
            get_microkernel,
            reset_microkernel_for_tests,
            ring_warmup_mutex,
        )
        from apex.warmup_progress import mark_warmup_ready, reset_warmup_progress
        from signals.indicators import compute_math_matrix
        from system.system_state import BootPhase, SystemState
        from trading.multi_api_broker import (
            MultiApiIngestionBroker,
            STREAM_A_INTERVAL_SEC,
            reset_multi_api_broker_for_tests,
        )

        self.assertIsInstance(ring_warmup_mutex(), threading.Lock)
        reset_microkernel_for_tests()
        reset_multi_api_broker_for_tests()
        SystemState.reset_singleton_for_tests()
        state = SystemState.get()
        state.update_state(BootPhase.WARMING, 80, "Compiling Vector Arrays", ready=False)
        reset_warmup_progress(bars_target=256)

        kernel = get_microkernel()
        kernel.start()

        broker = MultiApiIngestionBroker()
        broker.set_stream_b_fetcher(lambda epic: {"volatility_pct": 0.22, "regime": "neutral"})
        broker.attach(kernel)
        self.assertEqual(STREAM_A_INTERVAL_SEC, 0.020)

        ring = kernel._ring_for(_EPIC)
        self.assertEqual(ring._count, 0)

        t0 = time.perf_counter()
        for i in range(_FLOOD):
            kernel.on_tick_ingest(_EPIC, _quote(2400.0 + i * 0.01))
        flood_sec = time.perf_counter() - t0
        self.assertLess(flood_sec, 2.0, "2000-tick flood must complete quickly")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if kernel.deferred_queue_depth() >= min(_FLOOD, 5120) - 10:
                break
            time.sleep(0.01)

        self.assertEqual(ring._count, 0, "ring must stay pristine during WARMING")
        self.assertEqual(kernel.stats().get("ingested", 0), 0)

        mark_warmup_ready()
        time.sleep(0.05)
        self.assertGreater(ring._count, 0, "ring populated after warmup flush")
        flush_ms = float(kernel.stats().get("deferred_flush_ms", 0.0))
        self.assertGreater(flush_ms, 0.0)
        self.assertEqual(DEFERRED_FLUSH_CHUNK, 500)

        close, high, low = ring.ordered_views()
        self.assertEqual(close.dtype, np.float64)

        bench_close = np.linspace(2400.0, 2500.0, 256, dtype=np.float64)
        bench_high = bench_close + 0.4
        bench_low = bench_close - 0.4
        ind_buf = np.zeros((256, 4), dtype=np.float64)
        for _ in range(3):
            compute_math_matrix(bench_close, bench_high, bench_low, out_indicator_matrix=ind_buf)
        samples: list[float] = []
        for _ in range(32):
            t_math = time.perf_counter()
            snap = compute_math_matrix(
                bench_close, bench_high, bench_low, out_indicator_matrix=ind_buf
            )
            samples.append((time.perf_counter() - t_math) * 1_000_000.0)
        math_us = min(samples)
        self.assertLess(math_us, 250.0, f"compute_math_matrix best-of-32: {math_us:.1f}µs")
        self.assertEqual(snap["indicator_matrix"].dtype, np.float64)

        kernel.stop()

    def test_broker_stream_aggregation_enqueues_worker_a(self) -> None:
        os.environ["NODE_ENV"] = "production"
        os.environ["IG_MULTI_API_BROKER"] = "0"

        from apex.microkernel import get_microkernel, reset_microkernel_for_tests
        from trading.multi_api_broker import MultiApiIngestionBroker, reset_multi_api_broker_for_tests

        reset_microkernel_for_tests()
        reset_multi_api_broker_for_tests()
        kernel = get_microkernel()
        kernel.start()

        broker = MultiApiIngestionBroker()
        seq = {"n": 0}

        def _hook(epic: str) -> tuple[float, float]:
            seq["n"] += 1
            base = 100.0 + seq["n"] * 0.1
            return base, base + 0.5

        broker.set_stream_a_hook(_hook)
        broker.set_stream_b_fetcher(lambda e: {"volatility_pct": 0.5, "regime": "elevated"})
        broker.attach(kernel)
        broker.start(epics=[_EPIC])
        time.sleep(0.12)
        broker.stop()
        kernel.stop()

        stats = broker.stats()
        self.assertGreater(stats.get("stream_a_ticks", 0), 0)
        self.assertGreater(stats.get("aggregated_enqueued", 0), 0)


if __name__ == "__main__":
    unittest.main()
