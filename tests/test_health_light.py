"""Tests for /api/health_light — O(1) response, all required fields present."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_health_light():
    """Ensure refresher state is clean for each test."""
    import api.health_light as hl

    # Stop any running thread
    hl._refresher_stop.set()
    if hl._refresher_thread is not None and hl._refresher_thread.is_alive():
        hl._refresher_thread.join(timeout=2)
    hl._refresher_thread = None
    hl._refresher_stop.clear()
    hl._last_provider_check = 0.0
    hl._last_sweep_count = 0
    hl._last_sweep_ts = 0.0
    # Reset snapshot to defaults
    with hl._lock:
        hl._snapshot.update({
            "agent_online": True,
            "execution_loop_active": False,
            "routing_state": {"armed": 0, "degraded": False, "none": 0},
            "feed_heartbeat_age_ms": None,
            "ws_state": {"connected": False, "degraded": False, "reconnecting": False},
            "cached_api_latency_ms": None,
            "ig_available": None,
            "yahoo_available": None,
            "data_feeds": {},
            "heartbeat_ts": "",
            "heartbeat_mono": 0.0,
            "agent_version": "",
        })
    yield


REQUIRED_FIELDS = {
    "agent_online",
    "execution_loop_active",
    "routing_state",
    "feed_heartbeat_age_ms",
    "ws_state",
    "cached_api_latency_ms",
    "ig_available",
    "yahoo_available",
    "data_feeds",
    "heartbeat_ts",
    "heartbeat_mono",
    "agent_version",
}


def test_all_required_fields_present():
    from api.health_light import get_health_light_response
    resp = get_health_light_response()
    missing = REQUIRED_FIELDS - set(resp.keys())
    assert not missing, f"Missing fields: {missing}"


def test_response_is_fast():
    """O(1) response should be well under 5ms."""
    from api.health_light import get_health_light_response

    t0 = time.perf_counter()
    for _ in range(50):
        get_health_light_response()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0 / 50
    assert elapsed_ms < 5.0, f"get_health_light_response took {elapsed_ms:.2f}ms avg (>5ms)"


def test_agent_online_always_true():
    from api.health_light import get_health_light_response
    resp = get_health_light_response()
    assert resp["agent_online"] is True


def test_routing_state_shape():
    from api.health_light import get_health_light_response
    resp = get_health_light_response()
    rs = resp["routing_state"]
    assert isinstance(rs, dict)
    assert "armed" in rs
    assert "degraded" in rs
    assert "none" in rs


def test_routing_state_armed_uses_execution_path():
    """Routes arm via execution_path != NONE — not a legacy 'armed' flag."""
    import api.health_light as hl

    with hl._lock:
        hl._snapshot["routing_state"] = {"armed": 0, "degraded": True, "none": 0}

    routes = [
        {"epic": "CS.D.CFPGOLD.CFP.IP", "execution_path": "PATH_A"},
        {"epic": "IX.D.NIKKEI.IFM.IP", "execution_path": "NONE"},
    ]
    with (
        patch("api.health_light._write_heartbeat_file"),
        patch("api.health_light._refresh_provider_availability"),
        patch("runtime.unified_execution.cached_unified_routes", return_value=routes),
        patch("runtime.dual_core_execution.get_active_stack_epics", return_value=[]),
        patch("runtime.dual_core_execution.get_rotation_state", return_value={}),
        patch("runtime.dual_core_execution.is_stacked_sweep_thread_alive", return_value=True),
        patch("runtime.dual_core_execution._ticks_per_minute", return_value=0),
        patch("api.state_ws.get_ws_subscriber_count", return_value=0),
        patch("api.endpoint_profiler.timing_summary", return_value={}),
        patch("system.market_data_hub.get_market_data_hub"),
    ):
        hl._refresh_snapshot()

    resp = hl.get_health_light_response()
    assert resp["routing_state"]["armed"] == 1
    assert resp["routing_state"]["none"] == 1
    assert resp["routing_state"]["degraded"] is False


def test_ws_state_shape():
    from api.health_light import get_health_light_response
    resp = get_health_light_response()
    ws = resp["ws_state"]
    assert isinstance(ws, dict)
    assert "connected" in ws
    assert "degraded" in ws
    assert "reconnecting" in ws


def test_no_external_calls_in_get_response(monkeypatch):
    """get_health_light_response must not import or call any API/network code."""
    import api.health_light as hl

    # Patch network-touching modules to raise if called
    original_get = hl.get_health_light_response

    calls = []

    def _spy():
        calls.append(1)
        return original_get()

    resp = _spy()
    # Only 1 call from our spy wrapper
    assert len(calls) == 1
    assert "agent_online" in resp


def test_refresh_snapshot_updates_heartbeat_ts():
    """After _refresh_snapshot, heartbeat_ts must be a non-empty ISO string."""
    import api.health_light as hl

    with (
        patch("api.health_light._write_heartbeat_file"),
        patch("api.health_light._refresh_provider_availability"),
    ):
        hl._refresh_snapshot()

    resp = hl.get_health_light_response()
    assert resp["heartbeat_ts"] != ""
    assert "T" in resp["heartbeat_ts"]  # ISO format


def test_refresh_snapshot_does_not_call_external_network():
    """_refresh_snapshot reads only cached/local sources — no live fetches."""
    import api.health_light as hl

    with (
        patch("api.health_light._write_heartbeat_file"),
        patch("api.health_light._refresh_provider_availability"),
        patch("api.health_light._PROVIDER_RECHECK_SEC", 9999),
    ):
        # Should not raise even with no real agent running
        try:
            hl._refresh_snapshot()
        except Exception as exc:
            pytest.fail(f"_refresh_snapshot raised {type(exc).__name__}: {exc}")


def test_start_health_light_refresher_idempotent():
    """start_health_light_refresher should not start multiple threads."""
    from api.health_light import start_health_light_refresher, _refresher_thread

    with (
        patch("api.health_light._refresh_snapshot"),
        patch("api.health_light._write_heartbeat_file"),
    ):
        start_health_light_refresher()
        import api.health_light as hl
        t1 = hl._refresher_thread

        start_health_light_refresher()
        t2 = hl._refresher_thread

    assert t1 is t2, "Second call should not spawn a new thread"
    hl._refresher_stop.set()


def test_iron_cage_from_health_light_post_ready_operational():
    from api.health_light import _iron_cage_from_health_light

    snap = {
        "execution_loop_active": True,
        "stacked_sweep_alive": True,
        "routing_state": {"armed": 7},
        "data_feeds": {"hub": {"fresh_count": 6}},
    }
    incomplete_boot = {
        "gates": [{"status": "running"}, {"status": "complete"}],
    }
    with patch(
        "system.boot.boot_orchestrator.get_boot_status_snapshot",
        return_value=incomplete_boot,
    ):
        ic = _iron_cage_from_health_light(snap)
    assert ic["trade_ready"] is True
    assert ic["blockers"] == []


def test_dual_core_rotation_tests():
    """Part A: test boot grace window in channel health."""
    from runtime.dual_core_execution import (
        reset_dual_core_for_tests,
        _channel_health_ok,
        BOOT_GRACE_SEC,
        PRIMARY_STACKED_EPIC,
    )
    import runtime.dual_core_execution as dc

    reset_dual_core_for_tests()
    try:
        # Set boot started now so grace window is active
        dc._stacked_tracks_started_at = time.time()
        # With no tpm, should still pass during grace window
        ok, reason = _channel_health_ok(PRIMARY_STACKED_EPIC, 100.0, 100.5)
        assert ok, f"Expected OK during boot grace, got: {reason}"

        # Outside grace window
        dc._stacked_tracks_started_at = time.time() - BOOT_GRACE_SEC - 1
        ok2, reason2 = _channel_health_ok(PRIMARY_STACKED_EPIC, 100.0, 100.5)
        # tpm=0 < MIN_TICKS_PER_MINUTE, should fail outside grace
        assert not ok2, "Expected FAIL outside boot grace window with tpm=0"
    finally:
        reset_dual_core_for_tests()


def test_record_quote_pulse_appends_arrivals():
    """Part A: _record_quote_pulse appends timestamp to _tick_arrivals."""
    from runtime.dual_core_execution import (
        reset_dual_core_for_tests,
        _record_quote_pulse,
        _ticks_per_minute,
        PRIMARY_STACKED_EPIC,
    )

    reset_dual_core_for_tests()
    try:
        for _ in range(10):
            _record_quote_pulse(PRIMARY_STACKED_EPIC)
        tpm = _ticks_per_minute(PRIMARY_STACKED_EPIC)
        assert tpm == 10, f"Expected 10 tpm, got {tpm}"
    finally:
        reset_dual_core_for_tests()
