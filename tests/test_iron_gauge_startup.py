"""Iron Gauge startup cage — phase order, tier, stall recovery."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from system.boot.iron_gauge import (
    GaugePhase,
    PhaseStatus,
    evaluate_startup_tier,
    get_iron_gauge_snapshot,
    iron_gauge_mark,
    iron_gauge_tick,
    reset_iron_gauge_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_gauge():
    reset_iron_gauge_for_tests()
    yield
    reset_iron_gauge_for_tests()


def test_post_ready_critical_order_marks():
    iron_gauge_mark(GaugePhase.G5, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_EXECUTION_PLANE, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_HEALTH_LIGHT, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_ORCHESTRATOR, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_KERNEL, PhaseStatus.OK)
    snap = get_iron_gauge_snapshot()
    assert snap["post_ready_critical_met"] is True
    assert snap["post_ready_critical_index"] == 3


def test_evaluate_startup_tier_green_when_sealed():
    iron_gauge_mark(GaugePhase.G5, PhaseStatus.OK)
    for phase in (
        GaugePhase.POST_EXECUTION_PLANE,
        GaugePhase.POST_HEALTH_LIGHT,
        GaugePhase.POST_ORCHESTRATOR,
        GaugePhase.POST_KERNEL,
    ):
        iron_gauge_mark(phase, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.TELEMETRY, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.COMPLETE, PhaseStatus.OK)

    with patch(
        "api.health_light.get_health_light_response",
        return_value={
            "agent_online": True,
            "execution_loop_active": True,
            "routing_state": {"armed": 7},
            "data_feeds": {"hub": {"fresh_count": 7}},
        },
    ):
        assert evaluate_startup_tier(port=8080) == "green"


def test_stall_recovery_dispatches_orchestrator_lazy_arm():
    with patch(
        "runtime.master_orchestrator.ensure_orchestrator_armed_lazy",
        return_value=True,
    ) as lazy:
        from system.boot.iron_gauge import _dispatch_recovery

        assert _dispatch_recovery(GaugePhase.POST_ORCHESTRATOR.value) is True
        lazy.assert_called()


def test_snapshot_includes_phases_and_tier():
    iron_gauge_mark(GaugePhase.BIND, PhaseStatus.OK)
    snap = get_iron_gauge_snapshot()
    assert snap["ok"] is True
    assert "phases" in snap
    assert snap["tier"] in ("green", "amber", "red")
    assert "recovery_log" in snap
    assert "blockers" in snap
    assert "launcher_step" in snap
    assert 1 <= snap["launcher_step"] <= 9
    assert "orchestrator" in snap


def test_sealed_snapshot_when_complete_marked():
    iron_gauge_mark(GaugePhase.COMPLETE, PhaseStatus.OK, detail="test_seal")
    snap = get_iron_gauge_snapshot()
    assert snap["sealed"] is True
    assert evaluate_startup_tier(port=8080) == "green"


def test_sealed_when_kernel_still_running():
    iron_gauge_mark(GaugePhase.G5, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_EXECUTION_PLANE, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_HEALTH_LIGHT, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_ORCHESTRATOR, PhaseStatus.OK)
    iron_gauge_mark(GaugePhase.POST_KERNEL, PhaseStatus.RUNNING, detail="background_scheduled")
    iron_gauge_mark(GaugePhase.TELEMETRY, PhaseStatus.OK)

    with patch.dict("os.environ", {"IG_AGENT_PYTEST": ""}), patch(
        "system.boot.iron_gauge._sync_gates_from_system_state",
        lambda: None,
    ), patch(
        "system.boot.iron_gauge._sync_orchestrator_telemetry",
        lambda: None,
    ), patch(
        "system.boot.iron_gauge._detect_stalled_phases",
        lambda: [],
    ), patch(
        "api.health_light.get_health_light_response",
        return_value={
            "execution_loop_active": True,
            "routing_state": {"armed": 7},
            "data_feeds": {"hub": {"fresh_count": 7}},
        },
    ):
        iron_gauge_tick()
        snap = get_iron_gauge_snapshot()
        assert snap["sealed"] is True


def test_blockers_list_pending_gates():
    snap = get_iron_gauge_snapshot()
    assert any("gauge:G1" in b for b in snap["blockers"])
