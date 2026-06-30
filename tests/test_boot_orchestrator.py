"""Boot orchestrator and boot_status API tests."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from system.boot.boot_orchestrator import (
    BootStage,
    StepStatus,
    SubsystemId,
    get_boot_log_entries,
    get_boot_status_snapshot,
    init_boot_pipeline,
    mark_stage_ok,
    mark_subsystem,
    record_boot_event,
    reset_boot_orchestrator_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_boot():
    reset_boot_orchestrator_for_tests()
    yield
    reset_boot_orchestrator_for_tests()


def test_init_boot_pipeline_stage_a_running():
    snap = get_boot_status_snapshot()
    assert snap.get("current_stage") == BootStage.A.value
    stages = {s["id"]: s for s in snap.get("stages", [])}
    assert stages["A"]["status"] == StepStatus.RUNNING.value


def test_mark_stage_ok_updates_snapshot():
    mark_stage_ok(BootStage.A)
    stages = {s["id"]: s for s in get_boot_status_snapshot().get("stages", [])}
    assert stages["A"]["status"] == StepStatus.OK.value


def test_boot_log_records_events():
    record_boot_event("test_event", stage="A", detail="unit")
    entries = get_boot_log_entries(limit=10)
    assert any(e.get("event") == "test_event" for e in entries)


def test_sync_from_health_light_marks_subsystems():
    from system.boot.boot_orchestrator import _sync_from_cached_sources

    hl = {
        "agent_online": True,
        "ig_available": True,
        "yahoo_available": True,
        "execution_loop_active": True,
        "stacked_sweep_alive": True,
        "rotation_sweep_count": 42,
        "feed_stall": False,
        "routing_state": {"armed": 7, "degraded": False},
        "data_feeds": {"hub": {"fresh_count": 4, "total": 7}},
        "stack_tpm": {"IX.D.DOW.IFM.IP": 100},
    }
    with (
        patch("api.health_light.get_health_light_response", return_value=hl),
        patch(
            "system.system_state.get_system_state"
        ) as mock_ss,
        patch("cockpit.emergency.COCKPIT_EMERGENCY_OVERRIDE_ACTIVE", False),
    ):
        mock_ss.return_value.snapshot.return_value = {"ready": True, "phase": "READY"}
        _sync_from_cached_sources()
    snap = get_boot_status_snapshot()
    subs = {s["id"]: s for s in snap.get("subsystems", [])}
    assert subs[SubsystemId.FEEDS.value]["status"] == StepStatus.OK.value
    assert subs[SubsystemId.EXECUTION.value]["status"] == StepStatus.OK.value


def test_boot_status_api_is_fast_copy():
    from api.boot_status import get_boot_status_response

    mark_subsystem(SubsystemId.YAHOO, StepStatus.OK)
    body = get_boot_status_response()
    assert body.get("ok") is True
    assert "subsystems" in body


def test_subsystem_healer_cooldown():
    from system.boot.subsystem_healer import heal_routing, run_targeted_heal

    with patch("system.boot.subsystem_healer.heal_routing", return_value=True) as mock_heal:
        assert run_targeted_heal("routing") is True
        mock_heal.assert_called_once()
    # Second call within cooldown should no-op inside heal_routing itself
    t0 = time.monotonic()
    heal_routing()
    heal_routing()
    assert time.monotonic() - t0 < 1.0
