"""Tests for live agent state telemetry (/api/state, /ws/state)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.agent_state import (  # noqa: E402
    get_agent_state,
    get_api_state_response,
    record_loop_tick,
    reset_agent_state_for_tests,
    subscribe_state,
)
from system.system_state import (  # noqa: E402
    BootPhase,
    GateSnapshot,
    GateStatus,
    SystemState,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_agent_state_for_tests()
    SystemState.reset_singleton_for_tests()
    yield
    reset_agent_state_for_tests()
    SystemState.reset_singleton_for_tests()


def test_record_loop_tick_updates_version_and_gates() -> None:
    state = SystemState.get()
    state.update_state(
        phase=BootPhase.G2,
        percent=40,
        label="hydrating",
        gates_dict={
            "G1": GateSnapshot(status=GateStatus.COMPLETE),
            "G2": GateSnapshot(status=GateStatus.RUNNING),
        },
    )
    with patch("api.agent_state._feeds_from_hub", return_value=[{"epic": "X", "fresh": True}]):
        with patch("api.agent_health.get_runtime_tick_fields", return_value={"trading_healthy": True}):
            record_loop_tick(epic="CS.D.EURUSD.CFD.IP", bid=1.1, offer=1.1002, latency_ms=2.0)

    body = get_agent_state()
    assert body["version"] == 1
    assert body["gate_progression"]["gates"]["G1"] == "complete"
    assert body["feeds"][0]["epic"] == "X"
    assert body["runtime"]["trading_healthy"] is True


def test_get_api_state_response_under_100ms() -> None:
    with patch(
        "api.agent_state.get_agent_state",
        return_value={
            "version": 5,
            "updated_at": "2026-06-25T12:00:00.000Z",
            "legacy": {"bid": 1.2, "offer": 1.2001},
            "feeds": [],
            "routing": [],
            "gate_progression": {"phase": "G4"},
        },
    ):
        t0 = time.perf_counter()
        for _ in range(100):
            resp = get_api_state_response()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0 / 100.0
    assert resp["bid"] == 1.2
    assert resp["gate_progression"]["phase"] == "G4"
    assert elapsed_ms < 100.0


def test_subscribe_state_notifies_on_tick() -> None:
    seen: list[dict] = []
    unsub = subscribe_state(seen.append)
    with patch("api.agent_state._feeds_from_hub", return_value=[]):
        with patch("api.agent_health.get_runtime_tick_fields", return_value={}):
            record_loop_tick(epic="CS.D.EURUSD.CFD.IP", bid=1.0, offer=1.0001)
    unsub()
    assert len(seen) == 1
    assert seen[0]["version"] == 1


def test_routing_from_route_cache() -> None:
    from runtime.unified_execution import reset_unified_execution_for_tests, set_unified_execution_routes_for_tests

    reset_unified_execution_for_tests()
    set_unified_execution_routes_for_tests(
        [{"epic": "CS.D.EURUSD.CFD.IP", "execution_path": "MICRO", "route_confidence": 80}]
    )
    with patch("api.agent_state.get_tick", return_value={"positions": []}):
        with patch("api.readiness_snapshot.get_gui_snapshot", return_value={"snapshot_warming": True}):
            from api.agent_state import _merge_advisory_fields

            _merge_advisory_fields()
    body = get_agent_state()
    assert len(body["routing"]) == 1
    assert body["routing"][0]["execution_path"] == "MICRO"
