"""Stress tests for readiness endpoints under concurrent load."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.readiness_snapshot import (  # noqa: E402
    get_gui_snapshot,
    get_health_snapshot,
    reset_readiness_snapshot_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_readiness_snapshot_for_tests()
    yield
    reset_readiness_snapshot_for_tests()


def test_concurrent_health_reads_under_load() -> None:
    from api.readiness_snapshot import _HEALTH_SNAPSHOT, _LOCK, _META

    with _LOCK:
        _HEALTH_SNAPSHOT.update({"status": "OPERATIONAL", "ready": True})
        _META["health_ts"] = time.time()

    latencies: list[float] = []
    errors: list[str] = []

    def _read() -> None:
        try:
            t0 = time.perf_counter()
            code, _ = get_health_snapshot()
            latencies.append((time.perf_counter() - t0) * 1000.0)
            if code != 200:
                errors.append(f"bad code {code}")
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_read) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors
    assert latencies
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    assert p95 < 200.0, f"p95={p95}ms"


def test_gui_snapshot_never_blocks_under_refresh_flag() -> None:
    from api.readiness_snapshot import _GUI_SNAPSHOT, _LOCK, _META

    with _LOCK:
        _GUI_SNAPSHOT.update({"readiness_level": 2, "snapshot_tier": "fast"})
        _META["gui_full_refreshing"] = True

    with patch("api.gui_status.build_gui_status", side_effect=lambda: time.sleep(30) or {}):
        t0 = time.perf_counter()
        body = get_gui_snapshot()
        elapsed = (time.perf_counter() - t0) * 1000.0
    assert elapsed < 200.0
    assert body.get("readiness_level") == 2
