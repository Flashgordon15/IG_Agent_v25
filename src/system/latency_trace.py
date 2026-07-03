"""
Tick-to-trade latency trace — pre-allocated ring buffer, zero-alloc hot path.

Stages: feed_hub, signal, decision, ig_rest
Aggregation runs only on snapshot read (never on execution thread hot path).
"""

from __future__ import annotations

import threading
import time
from typing import Any

_RING_SIZE = 512
_STAGE_FEED = 0
_STAGE_SIGNAL = 1
_STAGE_DECISION = 2
_STAGE_IG = 3
_STAGE_NAMES = ("feed_hub", "signal", "decision", "ig_rest")
_STAGE_INDEX = {name: i for i, name in enumerate(_STAGE_NAMES)}


class _TraceFrame:
    """Fixed-layout trace row — no per-tick dict allocation."""

    __slots__ = (
        "epic",
        "ts0",
        "ts1",
        "ts2",
        "ts3",
        "d01_ms",
        "d12_ms",
        "d23_ms",
        "total_ms",
        "complete",
    )

    def __init__(self) -> None:
        self.epic = ""
        self.ts0 = 0.0
        self.ts1 = 0.0
        self.ts2 = 0.0
        self.ts3 = 0.0
        self.d01_ms = 0.0
        self.d12_ms = 0.0
        self.d23_ms = 0.0
        self.total_ms = 0.0
        self.complete = False

    def reset(self, epic: str) -> None:
        self.epic = epic
        self.ts0 = 0.0
        self.ts1 = 0.0
        self.ts2 = 0.0
        self.ts3 = 0.0
        self.d01_ms = 0.0
        self.d12_ms = 0.0
        self.d23_ms = 0.0
        self.total_ms = 0.0
        self.complete = False

    def set_stage(self, stage_idx: int, ts: float) -> None:
        if stage_idx == 0:
            self.ts0 = ts
        elif stage_idx == 1:
            self.ts1 = ts
        elif stage_idx == 2:
            self.ts2 = ts
        elif stage_idx == 3:
            self.ts3 = ts

    def stage_ts(self, stage_idx: int) -> float:
        if stage_idx == 0:
            return self.ts0
        if stage_idx == 1:
            return self.ts1
        if stage_idx == 2:
            return self.ts2
        return self.ts3

    def finalize(self) -> None:
        if self.ts0 > 0 and self.ts1 > 0 and self.ts1 >= self.ts0:
            self.d01_ms = (self.ts1 - self.ts0) * 1000.0
        if self.ts1 > 0 and self.ts2 > 0 and self.ts2 >= self.ts1:
            self.d12_ms = (self.ts2 - self.ts1) * 1000.0
        if self.ts2 > 0 and self.ts3 > 0 and self.ts3 >= self.ts2:
            self.d23_ms = (self.ts3 - self.ts2) * 1000.0
        first = self.ts0 or self.ts1 or self.ts2 or self.ts3
        last = self.ts3 or self.ts2 or self.ts1 or self.ts0
        if first > 0 and last >= first:
            self.total_ms = (last - first) * 1000.0
        self.complete = True


# Pre-allocated ring — objects reused in place
_RING: list[_TraceFrame] = [_TraceFrame() for _ in range(_RING_SIZE)]
_ring_write = 0
_active: _TraceFrame | None = None
_lock = threading.Lock()
_summary_cache: dict[str, Any] = {
    "ok": True,
    "samples": 0,
    "stages_ms": {},
    "ts": 0.0,
}
_summary_dirty = True


def record_stage(*, epic: str, stage: str, mono_ts: float | None = None) -> None:
    """Record stage timestamp — in-place mutation only, no I/O, no aggregation."""
    stage_idx = _STAGE_INDEX.get(stage)
    if stage_idx is None:
        return
    ts = mono_ts if mono_ts is not None else time.monotonic()
    global _active, _ring_write, _summary_dirty
    with _lock:
        frame = _active
        if frame is None or frame.epic != epic:
            frame = _RING[_ring_write % _RING_SIZE]
            _ring_write += 1
            frame.reset(epic)
            _active = frame
        frame.set_stage(stage_idx, ts)
        _summary_dirty = True


def record_pipeline_complete(*, epic: str) -> None:
    """Finalize active frame — no aggregation on hot path."""
    global _active, _summary_dirty
    with _lock:
        frame = _active
        if frame is None or frame.epic != epic:
            return
        frame.finalize()
        _active = None
        _summary_dirty = True


def _aggregate_summary_unlocked() -> dict[str, Any]:
    totals: list[float] = []
    d01: list[float] = []
    d12: list[float] = []
    d23: list[float] = []
    for frame in _RING:
        if not frame.complete or frame.total_ms <= 0:
            continue
        totals.append(frame.total_ms)
        if frame.d01_ms > 0:
            d01.append(frame.d01_ms)
        if frame.d12_ms > 0:
            d12.append(frame.d12_ms)
        if frame.d23_ms > 0:
            d23.append(frame.d23_ms)
    totals.sort()
    stages_ms: dict[str, float] = {}
    if d01:
        stages_ms["feed_hub_to_signal_ms"] = round(sum(d01) / len(d01), 3)
    if d12:
        stages_ms["signal_to_decision_ms"] = round(sum(d12) / len(d12), 3)
    if d23:
        stages_ms["decision_to_ig_rest_ms"] = round(sum(d23) / len(d23), 3)
    n = len(totals)
    return {
        "ok": True,
        "samples": n,
        "p50_total_ms": round(totals[n // 2], 3) if n else None,
        "p95_total_ms": round(totals[int(n * 0.95)], 3) if n >= 2 else None,
        "stages_ms": stages_ms,
        "ts": time.time(),
    }


def get_latency_trace_snapshot() -> dict[str, Any]:
    global _summary_cache, _summary_dirty
    with _lock:
        if _summary_dirty:
            _summary_cache = _aggregate_summary_unlocked()
            _summary_dirty = False
        return dict(_summary_cache)


def reset_latency_trace_for_tests() -> None:
    global _active, _ring_write, _summary_dirty, _summary_cache
    with _lock:
        _active = None
        _ring_write = 0
        _summary_dirty = True
        for frame in _RING:
            frame.reset("")
        _summary_cache = {"ok": True, "samples": 0, "stages_ms": {}, "ts": 0.0}
