"""Regime-aware strategy selector (Phase 6 v36) tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.regime_aware_selector import (
    build_regime_aware_strategy_selector,
    decide_epic_regime_aware_selection,
    reset_regime_aware_selector_for_tests,
    set_regime_aware_selector_for_tests,
)
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_regime_aware_selector_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _selector(profile: str = "MOMENTUM", confidence: int = 60) -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_strategy_profile": profile,
        "confidence": confidence,
    }


def test_regime_alignment_primary_driver():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        regime_row={"regime_classification": "TREND", "regime_confidence": 80},
        alignment_row={"recommended_profile": "MOMENTUM", "alignment_confidence": 75},
        selector_row=_selector("SCALP", 55),
    )
    assert result["recommended_profile"] == "MOMENTUM"
    assert "REGIME_ALIGNMENT_PRIMARY" in result["selector_flags"]
    assert result["contributing_factors"]["regime_alignment"] == "MOMENTUM"


def test_performance_bias_secondary_blend():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        regime_row={"regime_classification": "CHOP", "regime_confidence": 50},
        alignment_row={"recommended_profile": "SCALP", "alignment_confidence": 55},
        weighting_advice={"recommended_bias": "MOMENTUM", "bias_confidence": 70},
        selector_row=_selector("SCALP", 55),
    )
    assert result["recommended_profile"] == "MOMENTUM"
    assert "PERFORMANCE_BIAS_SECONDARY" in result["selector_flags"]


def test_threshold_adjustment_applied():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector(),
        adaptive_thresholds={
            "threshold_adjustments": {
                "SOFT_BLOCK_THRESHOLD": 60.0,
                "HARD_BLOCK_THRESHOLD": 90.0,
                "SCALP_CONFIDENCE_THRESHOLD": 55.0,
            }
        },
    )
    assert "THRESHOLD_ADJUSTMENT_APPLIED" in result["selector_flags"]
    assert result["contributing_factors"]["threshold_adjustments"] == "loosened"


def test_session_safety_high_risk_shifts_profile():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        regime_row={"regime_classification": "TREND", "regime_confidence": 80},
        alignment_row={"recommended_profile": "MOMENTUM", "alignment_confidence": 75},
        session_review={"session_quality_score": 55, "session_risk_score": 70, "session_stability_score": 40},
        selector_row=_selector("MOMENTUM"),
    )
    assert result["recommended_profile"] == "SWING"
    assert "SESSION_SAFETY_ADJUST" in result["selector_flags"]


def test_transition_overrides_regime():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        regime_row={"regime_classification": "TREND", "regime_confidence": 85},
        alignment_row={"recommended_profile": "MOMENTUM", "alignment_confidence": 80},
        transition_row={
            "current_profile": "MOMENTUM",
            "target_profile": "SWING",
            "transition_confidence": 90,
        },
        selector_row=_selector("MOMENTUM"),
    )
    assert result["recommended_profile"] == "SWING"
    assert "TRANSITION_OVERRIDES" in result["selector_flags"]


def test_hard_enforcement_fallback():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        regime_row={"regime_classification": "TREND", "regime_confidence": 85},
        alignment_row={"recommended_profile": "MOMENTUM", "alignment_confidence": 80},
        hard_row={
            "active": True,
            "hard_allow_paths": [ExecutionPath.MICRO.value],
            "hard_block_paths": [ExecutionPath.PATH_A.value, ExecutionPath.PATH_B_HANDOFF.value],
            "enforcement_flags": ["SCALP_HARD_ENFORCEMENT"],
        },
        selector_row=_selector("MOMENTUM"),
    )
    assert result["recommended_profile"] == "SCALP"
    assert "HARD_ENFORCEMENT_FALLBACK" in result["selector_flags"]


def test_contributing_factors_populated():
    result = decide_epic_regime_aware_selection(
        "CS.D.EURUSD.CFD.IP",
        regime_row={"regime_classification": "CHOP", "regime_confidence": 75},
        alignment_row={"recommended_profile": "SCALP", "alignment_confidence": 70},
        weighting_advice={"recommended_bias": "SCALP", "bias_confidence": 65},
        session_review={"session_quality_score": 80, "session_risk_score": 25, "session_stability_score": 85},
        transition_row={"current_profile": "SCALP", "target_profile": "MOMENTUM", "transition_confidence": 50},
        selector_row=_selector("SCALP"),
    )
    factors = result["contributing_factors"]
    assert factors["regime_alignment"] == "SCALP"
    assert factors["performance_bias"] == "SCALP"
    assert factors["session_quality"] == 80
    assert factors["risk_state"] == 25
    assert "SCALP" in factors["transition_state"]


def test_build_selector_for_multiple_epics():
    rows = build_regime_aware_strategy_selector(
        trade_pipeline_health=[
            {"epic": "CS.D.EURUSD.CFD.IP"},
            {"epic": "CS.D.CFPGOLD.CFP.IP"},
        ],
        strategy_selector_advice=[
            _selector("MOMENTUM"),
            {"epic": "CS.D.CFPGOLD.CFP.IP", "recommended_strategy_profile": "SCALP", "confidence": 65},
        ],
    )
    assert len(rows) == 2
    assert {r["epic"] for r in rows} == {"CS.D.EURUSD.CFD.IP", "CS.D.CFPGOLD.CFP.IP"}


def test_gui_status_includes_regime_aware_selector(tmp_path, monkeypatch):
    scope = "ig:RAS1"
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
        return_value={
            "pipeline_governance": {"per_epic": []},
            "session_governance": {},
            "gui_alerts": [],
        },
    ), patch("api.gui_status.build_strategy_selector_advice", return_value=[_selector()]), patch(
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
        return_value=[],
    ), patch(
        "api.gui_status.build_api_feed_health",
        return_value={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
    ), patch(
        "api.gui_status.build_market_rotation_status",
        return_value={"active_markets": []},
    ), patch(
        "api.gui_status.build_session_review_bundle",
        return_value={
            "session_review": {"session_quality_score": 70, "session_risk_score": 30, "session_stability_score": 75},
            "loosening_advice": {},
            "self_reflection": {},
        },
    ), patch(
        "api.gui_status.build_adaptive_thresholds",
        return_value={"threshold_adjustments": {}, "adjustment_flags": []},
    ), patch(
        "api.gui_status.build_strategy_performance_bundle",
        return_value={"strategy_performance_memory": {}, "strategy_weighting_advice": {}},
    ), patch(
        "api.gui_status.build_regime_detection_bundle",
        return_value={
            "regime_detection": [{"epic": "CS.D.EURUSD.CFD.IP", "regime_confidence": 75, "regime_classification": "TREND"}],
            "regime_strategy_alignment": [{"epic": "CS.D.EURUSD.CFD.IP", "recommended_profile": "MOMENTUM", "alignment_confidence": 72}],
        },
    ):
        payload = build_gui_status()

    assert "regime_aware_strategy_selector" in payload
    assert isinstance(payload["regime_aware_strategy_selector"], list)
    assert payload["regime_aware_strategy_selector"][0]["epic"] == "CS.D.EURUSD.CFD.IP"


def test_no_execution_side_effects():
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_regime_aware_strategy_selector(
            trade_pipeline_health=[{"epic": "CS.D.EURUSD.CFD.IP"}],
            strategy_selector_advice=[_selector()],
        )
        live_exec.assert_not_called()


def test_override_hook():
    custom = [{"epic": "X", "recommended_profile": "ROTATION", "selector_confidence": 99}]
    set_regime_aware_selector_for_tests(custom)
    assert build_regime_aware_strategy_selector(trade_pipeline_health=[{"epic": "X"}]) == custom
