"""Timing instrumentation for /api/health and /api/gui_status hot paths."""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

from system.engine_log import log_engine

_SLOW_MS = 200.0
_LOG_SLOW_MS = 500.0
_MAX_SAMPLES = 256

_LOCK = threading.Lock()
_SAMPLES: dict[str, deque[float]] = {}
_LAST: dict[str, float] = {}
_COUNTS: dict[str, int] = {}


@contextmanager
def timed_section(name: str, *, log_slow_ms: float = _LOG_SLOW_MS) -> Iterator[None]:
    """Record section duration; log when above threshold (background builds only)."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        record_timing(name, elapsed_ms, log_slow_ms=log_slow_ms)


def record_timing(name: str, elapsed_ms: float, *, log_slow_ms: float = _LOG_SLOW_MS) -> None:
    with _LOCK:
        bucket = _SAMPLES.setdefault(name, deque(maxlen=_MAX_SAMPLES))
        bucket.append(elapsed_ms)
        _LAST[name] = elapsed_ms
        _COUNTS[name] = _COUNTS.get(name, 0) + 1
    if elapsed_ms >= log_slow_ms:
        log_engine(f"endpoint_profiler: {name} {elapsed_ms:.1f}ms (slow)")


def record_request(name: str, elapsed_ms: float) -> None:
    """Record HTTP handler latency — always tracked, log if > 200ms."""
    record_timing(f"request:{name}", elapsed_ms, log_slow_ms=_SLOW_MS)


def timing_summary(*, alert_p95_ms: float = 200.0) -> dict[str, Any]:
    """Rolling p50/p95/max per instrumented section."""
    out: dict[str, Any] = {}
    alerts: list[str] = []
    with _LOCK:
        for name, samples in _SAMPLES.items():
            if not samples:
                continue
            ordered = sorted(samples)
            n = len(ordered)
            p50 = ordered[n // 2]
            p95 = ordered[int(n * 0.95)] if n > 1 else ordered[-1]
            entry = {
                "count": _COUNTS.get(name, n),
                "last_ms": round(_LAST.get(name, 0.0), 2),
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "max_ms": round(ordered[-1], 2),
            }
            if name.startswith("request:") and p95 >= alert_p95_ms:
                entry["alert"] = True
                alerts.append(f"{name} p95 {p95:.1f}ms >= {alert_p95_ms:.0f}ms")
            out[name] = entry
    if alerts:
        out["_alerts"] = alerts
    return out


def reset_profiler_for_tests() -> None:
    with _LOCK:
        _SAMPLES.clear()
        _LAST.clear()
        _COUNTS.clear()
