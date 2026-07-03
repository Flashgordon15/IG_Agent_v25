"""Iron Cage readiness contract tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from runtime import master_orchestrator as mo
from system.iron_cage_readiness import evaluate_iron_cage_readiness, reset_iron_cage_cache_for_tests


def _gate(status: str) -> MagicMock:
    g = MagicMock()
    g.status.value = status
    g.detail = ""
    return g


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_iron_cage_cache_for_tests()
    yield
    reset_iron_cage_cache_for_tests()


def test_orchestrator_degraded_warmup_does_not_block_trade_ready():
    snap = MagicMock()
    snap.ready = True
    snap.gate_completed_at = {f"G{i}": "t" for i in range(1, 6)}
    snap.gates = {f"G{i}": _gate("complete") for i in range(1, 6)}
    orch_snap = {
        "healthy": True,
        "degraded_override": True,
        "warming_up": True,
        "stage_status": {
            mo.STAGE_1_CONFIG_SANITY: "HEALTHY",
            mo.STAGE_3_REGIME_HYDRATION: "DEGRADED",
            mo.STAGE_4_TUNER_PRIME: "HEALTHY",
        },
        "stage_tokens": {
            mo.STAGE_1_CONFIG_SANITY: "SUCCESS",
            mo.STAGE_3_REGIME_HYDRATION: "WARMING",
            mo.STAGE_4_TUNER_PRIME: "SUCCESS",
            mo.STAGE_5_LAUNCH: "WARMING",
        },
        "phase_status": {
            mo.STAGE_1_CONFIG_SANITY: "HEALTHY",
            mo.STAGE_3_REGIME_HYDRATION: "DEGRADED",
        },
        "scoreboard": {"total_pp": 1000},
        "armed": True,
    }
    with patch("system.system_state.get_system_state") as gs:
        gs.return_value.snapshot_model.return_value = snap
        with patch("system.feeds.data_feed_orchestrator.get_data_feed_state", return_value={
            "health": "ok", "fresh_count": 7, "total_epics": 7, "primary_feed": "yahoo",
            "fallback_active": False, "retry_backoff_sec": 0,
        }):
            with patch("system.feeds.data_feed_orchestrator.ig_used_for_signal_path", return_value=False):
                with patch("system.iron_cage_readiness._read_execution_light_snapshot", return_value=(True, True, 5, 7)):
                    with patch("system.ig_budget_monitor.ig_budget_snapshot", return_value={"rate_limited": False, "cooldown_seconds_remaining": 0}):
                        with patch("api.agent_health._watchdog_active", return_value=True):
                            with patch("system.boot.boot_orchestrator.get_boot_status_snapshot", return_value={"trade_ready": True, "blockers": []}):
                                with patch("runtime.master_orchestrator.is_orchestrator_armed", return_value=True):
                                    with patch("runtime.master_orchestrator.is_orchestrator_primed", return_value=True):
                                        with patch("runtime.master_orchestrator.all_warmup_phases_acceptable", return_value=True):
                                            with patch("runtime.master_orchestrator.get_orchestrator_state_snapshot", return_value=orch_snap):
                                                with patch("system.alert_reporting_matrix.reporting_healthy", return_value=True):
                                                    out = evaluate_iron_cage_readiness(force_refresh=True)
    assert out["trade_ready"] is True
    assert "warmup_STAGE_3_REGIME_HYDRATION_degraded" in out["warnings"]
    assert "master_orchestrator_not_primed" not in out["blockers"]


def test_trade_ready_false_when_gates_incomplete():
    snap = MagicMock()
    snap.ready = True
    snap.gate_completed_at = {}
    snap.gates = {
        "G1": _gate("complete"),
        "G2": _gate("complete"),
        "G3": _gate("running"),
        "G4": _gate("pending"),
        "G5": _gate("pending"),
    }
    with patch("system.system_state.get_system_state") as gs:
        gs.return_value.snapshot_model.return_value = snap
        with patch("system.feeds.data_feed_orchestrator.get_data_feed_state", return_value={
            "health": "ok", "fresh_count": 2, "total_epics": 7, "primary_feed": "yahoo",
            "fallback_active": False, "retry_backoff_sec": 0,
        }):
            with patch("system.feeds.data_feed_orchestrator.ig_used_for_signal_path", return_value=False):
                with patch("system.iron_cage_readiness._read_execution_light_snapshot", return_value=(False, False, 0, 0)):
                    with patch("system.ig_budget_monitor.ig_budget_snapshot", return_value={"rate_limited": False}):
                        with patch("api.agent_health._watchdog_active", return_value=True):
                            with patch("system.boot.boot_orchestrator.get_boot_status_snapshot", return_value={"trade_ready": False, "blockers": []}):
                                out = evaluate_iron_cage_readiness(force_refresh=True)
    assert out["trade_ready"] is False
    assert "gates_incomplete" in out["blockers"]


def test_post_ready_operational_bypasses_stale_gate_blockers():
    snap = MagicMock()
    snap.ready = False
    snap.gate_completed_at = {}
    snap.gates = {f"G{i}": _gate("running") for i in range(1, 6)}
    with patch("system.system_state.get_system_state") as gs:
        gs.return_value.snapshot_model.return_value = snap
        with patch("system.feeds.data_feed_orchestrator.get_data_feed_state", return_value={
            "health": "ok", "fresh_count": 7, "total_epics": 7, "primary_feed": "yahoo",
            "fallback_active": False, "retry_backoff_sec": 0,
        }):
            with patch("system.feeds.data_feed_orchestrator.ig_used_for_signal_path", return_value=False):
                with patch("system.iron_cage_readiness._read_execution_light_snapshot", return_value=(True, True, 5, 7)):
                    with patch("system.ig_budget_monitor.ig_budget_snapshot", return_value={"rate_limited": False}):
                        with patch("api.agent_health._watchdog_active", return_value=True):
                            with patch("system.boot.boot_orchestrator.get_boot_status_snapshot", return_value={"trade_ready": False, "blockers": ["gates_incomplete"]}):
                                out = evaluate_iron_cage_readiness(force_refresh=True)
    assert out["trade_ready"] is True
    assert "gates_incomplete" not in out["blockers"]
    assert "boot_not_ready" not in out["blockers"]


def test_trade_ready_true_when_all_checks_pass():
    snap = MagicMock()
    snap.ready = True
    snap.gate_completed_at = {f"G{i}": "t" for i in range(1, 6)}
    snap.gates = {f"G{i}": _gate("complete") for i in range(1, 6)}
    with patch("system.system_state.get_system_state") as gs:
        gs.return_value.snapshot_model.return_value = snap
        with patch("system.feeds.data_feed_orchestrator.get_data_feed_state", return_value={
            "health": "ok", "fresh_count": 7, "total_epics": 7, "primary_feed": "yahoo",
            "fallback_active": False, "retry_backoff_sec": 0,
        }):
            with patch("system.feeds.data_feed_orchestrator.ig_used_for_signal_path", return_value=False):
                with patch("system.iron_cage_readiness._read_execution_light_snapshot", return_value=(True, True, 5, 7)):
                    with patch("system.ig_budget_monitor.ig_budget_snapshot", return_value={"rate_limited": False, "cooldown_seconds_remaining": 0}):
                        with patch("api.agent_health._watchdog_active", return_value=True):
                            with patch("system.boot.boot_orchestrator.get_boot_status_snapshot", return_value={"trade_ready": True, "blockers": []}):
                                out = evaluate_iron_cage_readiness(force_refresh=True)
    assert out["trade_ready"] is True
    assert out["ok"] is True
    assert out["ready_to_trade"] is True


def test_ig_on_signal_path_blocks_trade_ready():
    snap = MagicMock()
    snap.ready = True
    snap.gate_completed_at = {}
    snap.gates = {f"G{i}": _gate("complete") for i in range(1, 6)}
    with patch("system.system_state.get_system_state") as gs:
        gs.return_value.snapshot_model.return_value = snap
        with patch("system.feeds.data_feed_orchestrator.get_data_feed_state", return_value={
            "health": "ok", "fresh_count": 7, "total_epics": 7, "primary_feed": "yahoo",
            "fallback_active": False, "retry_backoff_sec": 0,
        }):
            with patch("system.feeds.data_feed_orchestrator.ig_used_for_signal_path", return_value=True):
                with patch("system.iron_cage_readiness._read_execution_light_snapshot", return_value=(True, True, 1, 7)):
                    with patch("system.ig_budget_monitor.ig_budget_snapshot", return_value={"rate_limited": False}):
                        with patch("api.agent_health._watchdog_active", return_value=True):
                            with patch("system.boot.boot_orchestrator.get_boot_status_snapshot", return_value={"trade_ready": True, "blockers": []}):
                                out = evaluate_iron_cage_readiness(force_refresh=True)
    assert out["trade_ready"] is False
    assert "ig_on_signal_path" in out["blockers"]


def test_fast_iron_cage_status_uses_health_light_when_operational():
    from system.iron_cage_readiness import fast_iron_cage_status_snapshot

    hl = {
        "execution_loop_active": True,
        "stacked_sweep_alive": True,
        "rotation_sweep_count": 12,
        "routing_state": {"armed": 7},
        "data_feeds": {"hub": {"fresh_count": 6, "total": 7}},
        "iron_cage": {"trade_ready": True, "blockers": []},
    }
    with patch("api.health_light.get_health_light_response", return_value=hl):
        out = fast_iron_cage_status_snapshot()
    assert out["trade_ready"] is True
    assert out["blockers"] == []
    assert out["execution"]["routes_armed"] == 7


def test_fast_iron_cage_derives_from_fields_when_nested_stale():
    from system.iron_cage_readiness import fast_iron_cage_status_snapshot

    hl = {
        "execution_loop_active": True,
        "stacked_sweep_alive": True,
        "rotation_sweep_count": 4,
        "routing_state": {"armed": 3},
        "data_feeds": {"hub": {"fresh_count": 5, "total": 7}},
        "iron_cage": {"trade_ready": False, "blockers": ["feed_starvation"]},
    }
    with patch("api.health_light.get_health_light_response", return_value=hl):
        out = fast_iron_cage_status_snapshot()
    assert out["trade_ready"] is True
    assert out["source"] == "health_light_fast"
