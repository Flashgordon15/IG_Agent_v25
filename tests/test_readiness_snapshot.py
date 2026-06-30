"""Tests for async readiness snapshots — fast /api/health and /api/gui_status."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.readiness_snapshot import (  # noqa: E402
    get_gui_snapshot,
    get_health_snapshot,
    refresh_gui_snapshot,
    refresh_health_snapshot,
    reset_readiness_snapshot_for_tests,
    resolve_gate_progression,
)
from system.system_state import (  # noqa: E402
    BootPhase,
    GateSnapshot,
    GateStatus,
    SystemState,
)


@pytest.fixture(autouse=True)
def _reset_snapshots() -> None:
    reset_readiness_snapshot_for_tests()
    SystemState.reset_singleton_for_tests()
    yield
    reset_readiness_snapshot_for_tests()
    SystemState.reset_singleton_for_tests()


def _set_gates(**statuses: str) -> None:
    state = SystemState.get()
    gates = {
        gid: GateSnapshot(status=GateStatus(status))
        for gid, status in statuses.items()
    }
    state.update_state(
        phase=BootPhase.BOOTING,
        percent=10,
        label="test",
        gates_dict=gates,
    )


def test_gate_progression_includes_g1_g4() -> None:
    _set_gates(G1="complete", G2="running", G3="pending", G4="pending")
    prog = resolve_gate_progression()
    assert prog["gates"]["G1"] == "complete"
    assert prog["gates"]["G2"] == "running"
    assert prog["active_gate"] == 2
    assert prog["operational_ready"] is False


def test_health_warm_up_returns_200_not_503() -> None:
    _set_gates(G1="running", G2="pending", G3="pending")
    code, body = get_health_snapshot()
    assert code == 200
    assert body["status"] == "INITIALIZING"
    assert body["ready"] is False
    assert "gate_progression" in body


def test_health_hydrating_returns_200() -> None:
    _set_gates(G1="complete", G2="running", G3="pending")
    code, body = get_health_snapshot()
    assert code == 200
    assert body["status"] == "HYDRATING"
    assert body["gate"] == 2


def test_health_failed_returns_503() -> None:
    _set_gates(G1="failed", G2="pending", G3="pending")
    state = SystemState.get()
    state.update_state(phase=BootPhase.FAILED, percent=0, label="auth failed", error="auth failed")
    code, body = get_health_snapshot()
    assert code == 503
    assert body["status"] == "FAILED"


def test_health_snapshot_served_under_200ms() -> None:
    with patch(
        "api.readiness_snapshot._HEALTH_SNAPSHOT",
        {
            "status": "OPERATIONAL",
            "ready": True,
            "gate_progression": {"gates": {}},
        },
    ), patch("api.readiness_snapshot._META", {"health_ts": time.time()}):
        t0 = time.perf_counter()
        for _ in range(50):
            code, body = get_health_snapshot()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0 / 50.0
    assert code == 200
    assert elapsed_ms < 200.0


def test_gui_snapshot_never_blocks_on_build() -> None:
    def _slow_build() -> dict:
        time.sleep(5.0)
        return {"unified_execution_route": []}

    with patch("api.gui_status.build_gui_status", side_effect=_slow_build):
        t0 = time.perf_counter()
        body = get_gui_snapshot()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 200.0
    assert body.get("snapshot_warming") is True


def test_refresh_health_populates_snapshot() -> None:
    _set_gates(G1="complete", G2="complete", G3="complete")
    with patch("api.agent_health.refresh_health_cache", return_value={"loops_running": True}):
        refresh_health_snapshot()
    code, body = get_health_snapshot()
    assert code == 200
    assert body["status"] == "OPERATIONAL"
    assert body.get("snapshot_warming") is False


def test_refresh_gui_populates_snapshot() -> None:
    stub = {"unified_execution_route": [{"epic": "CS.D.EURUSD.CFD.IP"}], "api_feed_health": {}}
    with patch("api.gui_status.build_gui_status", return_value=stub):
        refresh_gui_snapshot()
    body = get_gui_snapshot()
    assert body.get("snapshot_warming") is False
    assert len(body.get("unified_execution_route") or []) == 1
    assert "gate_progression" in body
