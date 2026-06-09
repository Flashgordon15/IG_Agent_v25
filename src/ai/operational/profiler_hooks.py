"""Instrumentation hooks for trading loop and execution engine (§20.2)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def probe_hot_path(name: str, *, epic: str = "") -> Iterator[None]:
    """Read-only wrapper — records wall-clock latency without altering outcomes."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        try:
            from ai.operational.profiler import get_operational_profiler

            get_operational_profiler().record_probe(
                name,
                (time.perf_counter() - t0) * 1000.0,
                epic=epic,
            )
        except Exception:
            pass
