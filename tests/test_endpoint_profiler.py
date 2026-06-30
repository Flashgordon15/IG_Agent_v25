"""Tests for endpoint profiler and tiered readiness snapshots."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.endpoint_profiler import (  # noqa: E402
    record_request,
    record_timing,
    reset_profiler_for_tests,
    timed_section,
    timing_summary,
)
from api.readiness_snapshot import (  # noqa: E402
    _publish_gui_merged,
    get_gui_snapshot,
    get_health_snapshot,
    refresh_gui_fast_snapshot,
    reset_readiness_snapshot_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_profiler_for_tests()
    reset_readiness_snapshot_for_tests()
    yield
    reset_profiler_for_tests()
    reset_readiness_snapshot_for_tests()


def test_timed_section_records() -> None:
    with timed_section("test.sleep"):
        time.sleep(0.01)
    summary = timing_summary()
    assert "test.sleep" in summary
    assert summary["test.sleep"]["count"] >= 1


def test_request_timing_under_200ms() -> None:
    from api.readiness_snapshot import _HEALTH_SNAPSHOT, _LOCK, _META

    with _LOCK:
        _HEALTH_SNAPSHOT.update({"status": "OPERATIONAL", "ready": True})
        _META["health_ts"] = time.time()
    t0 = time.perf_counter()
    for _ in range(20):
        code, body = get_health_snapshot()
    elapsed = (time.perf_counter() - t0) * 1000.0 / 20.0
    assert code == 200
    assert elapsed < 200.0


def test_gui_fast_layer_served_before_full() -> None:
    stub_fast = {"readiness_level": 2, "api_feed_health": {"feeds": {}}, "snapshot_tier": "fast"}
    with patch("api.gui_status_fast.build_gui_status_fast", return_value=stub_fast):
        refresh_gui_fast_snapshot()
    body = get_gui_snapshot()
    assert body.get("readiness_level") == 2
    assert body.get("snapshot_tier") == "fast"


def test_merge_gui_layers_prefers_full() -> None:
    from api.readiness_snapshot import _GUI_LAYERS, _LOCK

    with _LOCK:
        _GUI_LAYERS["fast"] = {"readiness_level": 1, "api_feed_health": {}}
        _GUI_LAYERS["full"] = {"readiness_level": 4, "strategy_governance": {}}
    merged = _publish_gui_merged()
    assert merged["readiness_level"] == 4
    assert merged["snapshot_tier"] == "full"


def test_health_cache_stub_avoids_slow_fallback() -> None:
    from api.agent_health import get_cached_health_status

    body = get_cached_health_status(allow_slow_fallback=False)
    assert body.get("health_cache_warming") is True


def test_record_request_tracks_handler_latency() -> None:
    record_request("health", 12.5)
    summary = timing_summary()
    assert "request:health" in summary
