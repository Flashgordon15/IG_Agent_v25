"""Staged readiness model — G1–G4 levels and subsystem visibility."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.readiness_model import (  # noqa: E402
    build_readiness_bundle,
    gate_readiness_level,
)
from system.system_state import (  # noqa: E402
    BootPhase,
    GateSnapshot,
    GateStatus,
    SystemState,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    SystemState.reset_singleton_for_tests()
    yield
    SystemState.reset_singleton_for_tests()


def _gates(**statuses: str) -> dict[str, str]:
    return {gid: statuses.get(gid, "pending") for gid in ("G1", "G2", "G3", "G4", "G5")}


def test_gate_level_progression() -> None:
    assert gate_readiness_level(_gates(G1="complete")) == 1
    assert gate_readiness_level(_gates(G1="complete", G2="complete")) == 2
    assert gate_readiness_level(_gates(G1="complete", G2="complete", G3="complete")) == 3
    assert gate_readiness_level(_gates(G1="complete", G2="complete", G3="complete", G4="complete")) == 4
    assert gate_readiness_level(_gates(G1="failed")) == 0


def test_partial_ready_with_feeds_before_g3() -> None:
    bundle = build_readiness_bundle(
        gate_progression={
            "phase": "G2",
            "gates": _gates(G1="complete", G2="running"),
            "operational_ready": False,
        },
        api_feed_health={
            "feeds": {
                "feed1": {"status": "OK", "latency_ms": 10},
                "feed2": {"status": "DEGRADED", "latency_ms": 100},
            }
        },
        unified_execution_route=[],
        regime_risk_envelope=[{"risk_profile": "moderate"}],
    )
    assert bundle["readiness_level"] >= 2
    assert bundle["subsystem_readiness"]["feeds"]["ready"] is True
    assert bundle["cockpit_usable"] is True
    assert bundle["partial_ready"] is True
    assert bundle["trading_ready"] is False


def test_routing_warming_while_feeds_ready() -> None:
    bundle = build_readiness_bundle(
        gate_progression={
            "phase": "G3_STREAMING",
            "gates": _gates(G1="complete", G2="complete", G3="running"),
            "operational_ready": False,
        },
        api_feed_health={
            "feeds": {
                "feed1": {"status": "OK"},
                "feed2": {"status": "OK"},
                "feed3": {"status": "OK"},
                "feed4": {"status": "OK"},
            }
        },
        unified_execution_route=[
            {"epic": "CS.D.EURUSD.CFD.IP", "execution_path": "MICRO"},
        ],
    )
    assert bundle["readiness_label"] == "Platform warming"
    assert bundle["subsystem_readiness"]["routing"]["level"] in ("partial", "warming")
    assert bundle["subsystem_readiness"]["feeds"]["ready"] is True


def test_governance_restricted_does_not_mark_trading_ready() -> None:
    bundle = build_readiness_bundle(
        gate_progression={
            "phase": "READY",
            "gates": _gates(G1="complete", G2="complete", G3="complete", G4="complete"),
            "operational_ready": True,
        },
        api_feed_health={"feeds": {"f1": {"status": "OK"}, "f2": {"status": "OK"}, "f3": {"status": "OK"}, "f4": {"status": "OK"}}},
        unified_execution_route=[
            {"epic": "A", "execution_path": "MICRO"},
            {"epic": "B", "execution_path": "MICRO"},
            {"epic": "C", "execution_path": "MICRO"},
            {"epic": "D", "execution_path": "MICRO"},
        ],
        hard_enforcement_decisions=[{"epic": "A", "active": True, "reason": "cap"}],
    )
    assert bundle["readiness_level"] == 4
    assert bundle["subsystem_readiness"]["governance"]["level"] == "restricted"
    assert bundle["trading_ready"] is False


def test_failed_gate_cockpit_not_usable() -> None:
    state = SystemState.get()
    state.update_state(
        phase=BootPhase.FAILED,
        percent=0,
        label="auth failed",
        gates_dict={"G1": GateSnapshot(status=GateStatus.FAILED)},
    )
    bundle = build_readiness_bundle(
        gate_progression={
            "phase": "FAILED",
            "gates": _gates(G1="failed"),
        },
    )
    assert bundle["readiness_level"] == 0
    assert bundle["cockpit_usable"] is False
    assert bundle["readiness_label"] == "Boot failed"


def test_subsystem_feeds_uses_data_feed_orchestrator_counts() -> None:
    bundle = build_readiness_bundle(
        gate_progression={
            "phase": "G5",
            "gates": _gates(G1="complete", G2="complete", G3="complete", G4="complete"),
            "operational_ready": True,
        },
        api_feed_health={
            "fresh_count": 7,
            "total_epics": 7,
            "health": "ok",
            "feeds": {
                "yahoo": {"health": "ok", "alive": True, "last_tick_at": "2026-01-01T00:00:00Z"},
            },
        },
        unified_execution_route=[{"epic": "CS.D.EURUSD.CFD.IP", "execution_path": "MICRO"}],
    )
    feeds = bundle["subsystem_readiness"]["feeds"]
    assert feeds["ready"] is True
    assert feeds["fresh_count"] == 7
    assert feeds["total_count"] == 7
