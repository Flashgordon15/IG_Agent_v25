#!/usr/bin/env python3
"""
Capacity & Latency Stress Harness — telemetry bridge + WebSocket flood.

Blasts Pydantic-validated JSON through the telemetry queue and cockpit
WebSocket channels. Raises CapacityIntegrityError on duplicate seq, schema
drift, placeholder numeric fields, or invalid broker lot precision.

Run:
  PYTHONPATH=src python3 -m pytest tests/stress/capacity_throttle.py -v
  STRESS_RATE=5000 PYTHONPATH=src python3 tests/stress/capacity_throttle.py
"""

from __future__ import annotations

import json
import os
import queue
import sys
import time
import tracemalloc
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.queue_guard import put_drop_oldest
from cockpit.telemetry_bridge import get_telemetry_queue
from cockpit.web_server import _stop, create_cockpit_app
from stress.telemetry_packet import (
    CapacityIntegrityError,
    SchemaDriftTracker,
    TelemetryPacketGenerator,
)
from trading.position_ladder import (
    finalize_dispatch_lot_size,
    is_valid_broker_lot,
    truncate_to_broker_lot,
)

try:
    from starlette.testclient import TestClient
except ImportError:
    TestClient = None  # type: ignore[misc, assignment]


# Default automated pytest profile — maximum load contract (5k frames/sec target).
DEFAULT_TARGET_RATE = int(os.environ.get("STRESS_RATE", "5000"))
BURST_FRAMES = int(os.environ.get("STRESS_FRAMES", "5000"))
MAX_LOAD_FRAMES = 5000


class LotSizeIntegrityError(CapacityIntegrityError):
    """Fatal — simulated lot size violates IG two-decimal broker contract."""


def assert_broker_lot_boundary(size: float, *, context: str = "") -> float:
    """Fail immediately if *size* exceeds two decimal places."""
    if not is_valid_broker_lot(size):
        welded = truncate_to_broker_lot(size)
        raise LotSizeIntegrityError(
            f"lot size {size} exceeds two-decimal boundary "
            f"(expected {welded}){': ' + context if context else ''}"
        )
    return float(size)


def simulate_risk_scaling_lot_sizes(*, frames: int) -> list[float]:
    """
    Simulate stacked risk-band + overnight scaling events (incl. 1.125 bug path).
    Returns broker-welded sizes only.
    """
    from system.risk_bands import apply_risk_band_to_size

    welded: list[float] = []
    epic = "CS.D.EURUSD.CFD.IP"
    for i in range(frames):
        base = 1.5
        conf = 55.0 + (i % 45)
        banded, _band, _note = apply_risk_band_to_size(
            base,
            confidence=conf,
            stop_pts=8.0,
            point_value_gbp=1.0,
            epic_risk_cap_gbp=150.0,
        )
        overnight_mult = 0.75 if i % 3 == 0 else 1.0
        raw = float(banded) * overnight_mult
        final, _reason = finalize_dispatch_lot_size(
            raw,
            epic=epic,
            micro_confidence=0.72,
            apply_overnight_scale=False,
        )
        assert_broker_lot_boundary(final, context=f"frame={i} raw={raw}")
        welded.append(final)
    return welded


@dataclass
class ThrottleReport:
    target_rate: int
    frames_sent: int
    bytes_validated: int
    elapsed_sec: float
    achieved_rate: float
    peak_rss_mb: float
    queue_drops: int
    ws_frames: int
    lot_sizes_validated: int = 0


class CapacityThrottleHarness:
    """High-frequency packet generator with integrity enforcement."""

    def __init__(self, *, target_rate: int = DEFAULT_TARGET_RATE) -> None:
        self.target_rate = max(100, int(target_rate))
        self._generator = TelemetryPacketGenerator()
        self._tracker = SchemaDriftTracker()

    def run_lot_boundary_gate(self, *, frames: int = MAX_LOAD_FRAMES) -> int:
        """Explicit S2b gate — risk scaling must never emit >2 decimal lots."""
        welded = simulate_risk_scaling_lot_sizes(frames=frames)
        if len(welded) != frames:
            raise LotSizeIntegrityError(
                f"lot gate dropped frames: {len(welded)}/{frames}"
            )
        assert truncate_to_broker_lot(1.125) == 1.12
        return len(welded)

    def run_schema_flood(self, *, frames: int = BURST_FRAMES) -> ThrottleReport:
        tracemalloc.start()
        t0 = time.perf_counter()
        interval = 1.0 / float(self.target_rate)
        drops = 0
        lots_ok = 0

        for i in range(frames):
            seq, raw = self._generator.next_payload(pnl_delta=i * 0.001)
            try:
                norm = self._tracker.ingest(raw, seq=seq)
                assert_broker_lot_boundary(
                    float(norm["size"]), context=f"telemetry seq={seq}"
                )
                lots_ok += 1
            except CapacityIntegrityError:
                raise
            if interval > 0 and i % max(1, self.target_rate // 100) == 0:
                time.sleep(interval * max(1, self.target_rate // 100))

        elapsed = max(time.perf_counter() - t0, 1e-6)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return ThrottleReport(
            target_rate=self.target_rate,
            frames_sent=frames,
            bytes_validated=self._tracker.bytes_in,
            elapsed_sec=elapsed,
            achieved_rate=frames / elapsed,
            peak_rss_mb=peak / (1024 * 1024),
            queue_drops=drops,
            ws_frames=0,
            lot_sizes_validated=lots_ok,
        )

    def run_queue_burst(self, *, frames: int = 512) -> ThrottleReport:
        """Flood in-process telemetry queue — measures drop-oldest behaviour."""
        q = get_telemetry_queue()
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

        tracemalloc.start()
        t0 = time.perf_counter()
        before_qsize = q.qsize()
        sent = 0
        for i in range(frames):
            frame = self._generator.telemetry_frame(seq=i + 1)
            put_drop_oldest(q, frame)
            sent += 1

        elapsed = max(time.perf_counter() - t0, 1e-6)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        after_qsize = q.qsize()
        drops = max(0, sent - max(after_qsize - before_qsize, 0))
        return ThrottleReport(
            target_rate=self.target_rate,
            frames_sent=sent,
            bytes_validated=self._tracker.bytes_in,
            elapsed_sec=elapsed,
            achieved_rate=sent / elapsed,
            peak_rss_mb=peak / (1024 * 1024),
            queue_drops=drops,
            ws_frames=0,
        )

    def run_websocket_soak(self, *, reads: int = 12) -> ThrottleReport:
        if TestClient is None:
            raise unittest.SkipTest("starlette TestClient unavailable")

        _stop.clear()
        tracemalloc.start()
        t0 = time.perf_counter()
        ws_count = 0
        bytes_in = 0

        with TestClient(create_cockpit_app()) as client:
            with client.websocket_connect("/ws/telemetry") as ws_tel:
                for _ in range(reads):
                    payload = ws_tel.receive_json()
                    raw = json.dumps(payload, default=str)
                    bytes_in += len(raw.encode("utf-8"))
                    if payload.get("type") == "SYSTEM_HOT_RELOAD":
                        continue
                    ws_count += 1
                    for key in ("entry", "level", "profitAndLoss"):
                        if key in payload and payload[key] is not None:
                            if str(payload[key]).strip() in ("", "—", "N/A"):
                                raise CapacityIntegrityError(
                                    f"ws telemetry placeholder {key}"
                                )

            with client.websocket_connect("/ws/logs") as ws_log:
                for _ in range(min(reads, 6)):
                    frame = ws_log.receive_json()
                    if frame.get("type") != "LOG_FRAME":
                        raise CapacityIntegrityError("unexpected log frame type")
                    ws_count += 1

        elapsed = max(time.perf_counter() - t0, 1e-6)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return ThrottleReport(
            target_rate=self.target_rate,
            frames_sent=reads,
            bytes_validated=bytes_in,
            elapsed_sec=elapsed,
            achieved_rate=ws_count / elapsed,
            peak_rss_mb=peak / (1024 * 1024),
            queue_drops=0,
            ws_frames=ws_count,
        )


class CapacityThrottleTests(unittest.TestCase):
    def test_truncate_1125_block_error_fixed(self) -> None:
        self.assertAlmostEqual(truncate_to_broker_lot(1.125), 1.12)
        self.assertTrue(is_valid_broker_lot(1.12))
        self.assertFalse(is_valid_broker_lot(1.125))

    def test_lot_boundary_gate_max_load(self) -> None:
        harness = CapacityThrottleHarness(target_rate=DEFAULT_TARGET_RATE)
        count = harness.run_lot_boundary_gate(frames=MAX_LOAD_FRAMES)
        self.assertEqual(count, MAX_LOAD_FRAMES)

    def test_risk_scaling_never_exceeds_two_decimals(self) -> None:
        with self.assertRaises(LotSizeIntegrityError):
            assert_broker_lot_boundary(1.125, context="raw pre-weld must fail")
        welded = simulate_risk_scaling_lot_sizes(frames=200)
        self.assertEqual(len(welded), 200)
        for lot in welded:
            self.assertTrue(is_valid_broker_lot(lot))

    def test_schema_flood_no_duplicates_max_profile(self) -> None:
        harness = CapacityThrottleHarness(target_rate=DEFAULT_TARGET_RATE)
        report = harness.run_schema_flood(frames=MAX_LOAD_FRAMES)
        self.assertEqual(report.frames_sent, MAX_LOAD_FRAMES)
        self.assertGreater(report.bytes_validated, 0)
        self.assertEqual(report.lot_sizes_validated, MAX_LOAD_FRAMES)

    def test_duplicate_seq_raises_fatal(self) -> None:
        tracker = SchemaDriftTracker()
        gen = TelemetryPacketGenerator()
        seq, raw = gen.next_payload()
        tracker.ingest(raw, seq=seq)
        with self.assertRaises(CapacityIntegrityError):
            tracker.ingest(raw, seq=seq)

    def test_queue_burst_bounded_memory(self) -> None:
        harness = CapacityThrottleHarness(target_rate=DEFAULT_TARGET_RATE)
        report = harness.run_queue_burst(frames=800)
        self.assertGreater(report.achieved_rate, 1000)
        self.assertLess(report.peak_rss_mb, 64.0)

    def test_websocket_telemetry_and_logs(self) -> None:
        harness = CapacityThrottleHarness()
        report = harness.run_websocket_soak(reads=8)
        self.assertGreaterEqual(report.ws_frames, 8)

    def test_position_schema_fields_stable(self) -> None:
        tracker = SchemaDriftTracker()
        gen = TelemetryPacketGenerator()
        for _ in range(50):
            seq, raw = gen.next_payload()
            norm = tracker.ingest(raw, seq=seq)
            self.assertAlmostEqual(norm["entry"], norm["level"])
            self.assertIsInstance(norm["profitAndLoss"], float)
            assert_broker_lot_boundary(float(norm["size"]))


if __name__ == "__main__":
    rate = int(os.environ.get("STRESS_RATE", "5000"))
    frames = int(os.environ.get("STRESS_FRAMES", "5000"))
    harness = CapacityThrottleHarness(target_rate=rate)
    lot_count = harness.run_lot_boundary_gate(frames=frames)
    print(f"lot boundary gate: {lot_count} welded sizes @ two-decimal contract")
    report = harness.run_schema_flood(frames=frames)
    print(
        f"schema flood: {report.frames_sent} frames "
        f"@ {report.achieved_rate:.0f}/s peak={report.peak_rss_mb:.2f}MB "
        f"bytes={report.bytes_validated} lots={report.lot_sizes_validated}"
    )
    qreport = harness.run_queue_burst(frames=min(frames, 1024))
    print(f"queue burst: {qreport.achieved_rate:.0f}/s drops={qreport.queue_drops}")
    unittest.main()
