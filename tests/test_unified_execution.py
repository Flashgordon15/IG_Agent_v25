"""Unified execution engine (Phase 10 v40) tests — routing layer only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.regime_detection import MarketRegime
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath
from runtime.unified_execution import (
    UnifiedExecutionPath,
    build_unified_execution_routes,
    decide_epic_unified_route,
    reset_unified_execution_for_tests,
    set_unified_execution_routes_for_tests,
    unified_guard_micro_dispatch,
    unified_guard_path_a_execution,
    unified_guard_path_b_handoff,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_unified_execution_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _selector(profile: str = "MOMENTUM", confidence: int = 80) -> dict:
    return {"epic": "CS.D.EURUSD.CFD.IP", "recommended_profile": profile, "selector_confidence": confidence}


def _hard(allow: list[str] | None = None, block: list[str] | None = None, active: bool = True) -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "active": active,
        "hard_allow_paths": allow or [],
        "hard_block_paths": block or [],
    }


def test_scalp_routes_to_micro():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        hard_row=_hard(allow=[ExecutionPath.MICRO.value], block=[ExecutionPath.PATH_A.value]),
    )
    assert route["execution_path"] == UnifiedExecutionPath.MICRO.value
    assert "SCALP_ROUTE" in route["route_flags"]


def test_scalp_fallback_to_path_a_when_micro_blocked():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        hard_row=_hard(
            allow=[ExecutionPath.PATH_A.value],
            block=[ExecutionPath.MICRO.value],
        ),
    )
    assert route["execution_path"] == UnifiedExecutionPath.PATH_A.value
    assert "UNIFIED_FALLBACK_ROUTE" in route["route_flags"]


def test_momentum_routes_to_path_a():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        hard_row=_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value]),
    )
    assert route["execution_path"] == UnifiedExecutionPath.PATH_A.value
    assert "MOMENTUM_ROUTE" in route["route_flags"]


def test_momentum_fallback_to_micro():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        hard_row=_hard(
            allow=[ExecutionPath.MICRO.value],
            block=[ExecutionPath.PATH_A.value],
        ),
    )
    assert route["execution_path"] == UnifiedExecutionPath.MICRO.value
    assert "UNIFIED_FALLBACK_ROUTE" in route["route_flags"]


def test_swing_routes_to_path_a():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SWING"),
        hard_row=_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value]),
    )
    assert route["execution_path"] == UnifiedExecutionPath.PATH_A.value
    assert "SWING_ROUTE" in route["route_flags"]


def test_swing_blocked_returns_none():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SWING"),
        hard_row=_hard(allow=[], block=[ExecutionPath.PATH_A.value, ExecutionPath.MICRO.value]),
    )
    assert route["execution_path"] == UnifiedExecutionPath.NONE.value


def test_rotation_routes_to_path_b_sweep():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("ROTATION"),
        hard_row=_hard(allow=[ExecutionPath.PATH_B_HANDOFF.value], block=[ExecutionPath.PATH_A.value]),
        api_feed_health={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
    )
    assert route["execution_path"] == UnifiedExecutionPath.PATH_B_SWEEP.value
    assert "ROTATION_ROUTE" in route["route_flags"]


def test_rotation_fallback_to_micro():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("ROTATION"),
        hard_row=_hard(
            allow=[ExecutionPath.MICRO.value],
            block=[ExecutionPath.PATH_B_HANDOFF.value],
        ),
    )
    assert route["execution_path"] == UnifiedExecutionPath.MICRO.value


def test_stand_down_routes_to_none():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("STAND_DOWN"),
        hard_row=_hard(active=True, allow=[], block=[ExecutionPath.PATH_A.value, ExecutionPath.MICRO.value],),
    )
    assert route["execution_path"] == UnifiedExecutionPath.NONE.value
    assert "STAND_DOWN_ROUTE" in route["route_flags"]


def test_extreme_vol_regime_suppresses_route():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        regime_row={"regime_classification": MarketRegime.EXTREME_VOL.value, "regime_confidence": 90},
        hard_row=_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value]),
    )
    assert route["execution_path"] == UnifiedExecutionPath.NONE.value
    assert "REGIME_NONE_ROUTE" in route["route_flags"]


def test_daily_target_protection_suppresses_route():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        hard_row=_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value]),
        daily_targeting={
            "recommended_bias": {"stand_down_bias": 0.5},
            "bias_flags": ["AHEAD_OF_TARGET_PROTECTION"],
            "contributing_factors": {"session_progress": {"band": "ahead"}},
        },
    )
    assert route["execution_path"] == UnifiedExecutionPath.NONE.value
    assert "DAILY_TARGET_PROTECTION" in route["route_flags"]


def test_enforcement_stand_down_does_not_suppress_when_far_behind():
    """Hard-enforcement dampening raises stand_down_bias but must not block routes."""
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        hard_row=_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value]),
        daily_targeting={
            "recommended_bias": {"stand_down_bias": 0.7},
            "bias_flags": ["FAR_BEHIND_AGGRESSIVE", "ENFORCEMENT_BIAS_DAMPEN"],
            "contributing_factors": {"session_progress": {"band": "far_behind"}},
        },
    )
    assert route["execution_path"] != UnifiedExecutionPath.NONE.value
    assert "DAILY_TARGET_PROTECTION" not in route["route_flags"]


def test_unified_guards_enforce_cached_route():
    set_unified_execution_routes_for_tests(
        [{"epic": "CS.D.EURUSD.CFD.IP", "execution_path": "MICRO", "route_reason": "test"}]
    )
    assert unified_guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is True
    assert unified_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
    assert unified_guard_path_b_handoff("CS.D.EURUSD.CFD.IP") is False


def test_unified_guards_allow_when_no_cache():
    assert unified_guard_path_a_execution("CS.D.UNKNOWN") is True


def test_gui_status_includes_unified_execution_route(tmp_path, monkeypatch):
    scope = "ig:UNI1"
    root = tmp_path / "production"
    root.mkdir()
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(root))
    reset_app_mode_for_tests()
    write_session_lock(
        lock_path_for_scope(scope, root),
        pid=os.getpid(),
        port=8080,
        account_scope=scope,
    )

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[{"epic": "CS.D.EURUSD.CFD.IP"}]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value={"pipeline_governance": {"per_epic": []}, "session_governance": {}, "gui_alerts": []},
    ), patch("api.gui_status.build_strategy_selector_advice", return_value=[]), patch(
        "api.gui_status.build_strategy_controller_decisions",
        return_value=[],
    ), patch(
        "api.gui_status.build_strategy_transition_advice",
        return_value=[],
    ), patch(
        "api.gui_status.build_strategy_enforcement_decisions",
        return_value=[],
    ), patch(
        "api.gui_status.build_hard_enforcement_decisions",
        return_value=[_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value])],
    ), patch(
        "api.gui_status.build_api_feed_health",
        return_value={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
    ), patch(
        "api.gui_status.build_market_rotation_status",
        return_value={"active_markets": []},
    ), patch(
        "api.gui_status.build_session_review_bundle",
        return_value={"session_review": {}, "loosening_advice": {}, "self_reflection": {}},
    ), patch(
        "api.gui_status.build_adaptive_thresholds",
        return_value={"threshold_adjustments": {}, "adjustment_flags": []},
    ), patch(
        "api.gui_status.build_strategy_performance_bundle",
        return_value={"strategy_performance_memory": {}, "strategy_weighting_advice": {}},
    ), patch(
        "api.gui_status.build_regime_detection_bundle",
        return_value={
            "regime_detection": [{"epic": "CS.D.EURUSD.CFD.IP", "regime_classification": "TREND", "regime_confidence": 75}],
            "regime_strategy_alignment": [],
        },
    ), patch(
        "api.gui_status.build_regime_aware_strategy_selector",
        return_value=[_selector("MOMENTUM")],
    ), patch(
        "api.gui_status.build_regime_risk_envelope",
        return_value=[{"epic": "CS.D.EURUSD.CFD.IP", "risk_profile": "MEDIUM", "risk_confidence": 70}],
    ), patch(
        "api.gui_status.build_regime_sizing_advice",
        return_value=[{"epic": "CS.D.EURUSD.CFD.IP", "recommended_size_factor": 0.25, "sizing_confidence": 70}],
    ), patch(
        "api.gui_status.build_daily_pnl_targeting",
        return_value={"recommended_bias": {"stand_down_bias": 0.1}, "contributing_factors": {}},
    ):
        payload = build_gui_status()

    assert "unified_execution_route" in payload
    assert isinstance(payload["unified_execution_route"], list)
    assert payload["unified_execution_route"][0]["execution_path"] in (
        "MICRO",
        "PATH_A",
        "PATH_B_SWEEP",
        "NONE",
    )


def test_no_live_executor_on_route_build():
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_unified_execution_routes(
            trade_pipeline_health=[{"epic": "CS.D.EURUSD.CFD.IP"}],
            regime_aware_strategy_selector=[_selector()],
            hard_enforcement_decisions=[_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value])],
        )
        live_exec.assert_not_called()


def test_contributing_factors_in_route():
    route = decide_epic_unified_route(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        regime_row={"regime_classification": MarketRegime.TREND.value, "regime_confidence": 80},
        risk_row={"risk_profile": "MEDIUM", "risk_confidence": 70},
        sizing_row={"recommended_size_factor": 0.25, "sizing_confidence": 75},
        hard_row=_hard(allow=[ExecutionPath.PATH_A.value], block=[ExecutionPath.MICRO.value]),
    )
    factors = route["contributing_factors"]
    assert factors["strategy"] == "MOMENTUM"
    assert factors["regime"] == MarketRegime.TREND.value
    assert factors["risk"] == "MEDIUM"
    assert "enforcement" in factors
