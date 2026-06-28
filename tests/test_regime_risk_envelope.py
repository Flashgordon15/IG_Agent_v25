"""Regime-aware risk envelope (Phase 7 v37) tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.regime_detection import MarketRegime
from runtime.regime_risk_envelope import (
    build_regime_risk_envelope,
    decide_epic_risk_envelope,
    reset_regime_risk_envelope_for_tests,
    set_regime_risk_envelope_for_tests,
)
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_regime_risk_envelope_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _selector(profile: str = "MOMENTUM", confidence: int = 70) -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_profile": profile,
        "selector_confidence": confidence,
    }


def test_scalp_tight_risk_envelope():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        regime_row={"regime_classification": MarketRegime.CHOP.value, "regime_confidence": 70},
    )
    assert result["risk_profile"] == "TIGHT"
    assert "SCALP_TIGHT_RISK" in result["risk_flags"]


def test_scalp_tighter_under_high_volatility():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        regime_row={"regime_classification": MarketRegime.EXTREME_VOL.value, "regime_confidence": 85},
    )
    assert result["risk_profile"] == "TIGHT"
    assert "SCALP_TIGHTER_VOL" in result["risk_flags"]


def test_momentum_medium_risk_envelope():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        regime_row={"regime_classification": MarketRegime.UNKNOWN.value, "regime_confidence": 50},
    )
    assert result["risk_profile"] == "MEDIUM"
    assert "MOMENTUM_MEDIUM_RISK" in result["risk_flags"]


def test_momentum_wider_under_trend():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        regime_row={"regime_classification": MarketRegime.TREND.value, "regime_confidence": 80},
        performance_memory={"win_rates": {"momentum_win_rate": 62.0}},
    )
    assert result["risk_profile"] == "WIDE"
    assert "MOMENTUM_WIDER_TREND" in result["risk_flags"]


def test_momentum_tighter_under_chop():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        regime_row={"regime_classification": MarketRegime.CHOP.value, "regime_confidence": 75},
    )
    assert result["risk_profile"] == "TIGHT"
    assert "MOMENTUM_TIGHTER_CHOP_VOL" in result["risk_flags"]


def test_swing_wide_risk_envelope():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SWING"),
    )
    assert result["risk_profile"] == "WIDE"
    assert "SWING_WIDE_RISK" in result["risk_flags"]


def test_swing_tighter_under_extreme_vol():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SWING"),
        regime_row={"regime_classification": MarketRegime.EXTREME_VOL.value, "regime_confidence": 90},
    )
    assert result["risk_profile"] == "MEDIUM"
    assert "SWING_TIGHTER_EXTREME_LIQUIDITY" in result["risk_flags"]


def test_rotation_structural_risk_envelope():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("ROTATION"),
        regime_row={"regime_classification": MarketRegime.UNKNOWN.value, "regime_confidence": 50},
    )
    assert result["risk_profile"] == "STRUCTURAL"
    assert "ROTATION_STRUCTURAL_RISK" in result["risk_flags"]


def test_rotation_tighter_under_breakout():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("ROTATION"),
        regime_row={"regime_classification": MarketRegime.BREAKOUT.value, "regime_confidence": 80},
    )
    assert result["risk_profile"] == "WIDE"
    assert "ROTATION_TIGHTER_BREAKOUT" in result["risk_flags"]


def test_stand_down_zero_risk():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("STAND_DOWN"),
        hard_row={
            "active": True,
            "hard_allow_paths": [],
            "hard_block_paths": [ExecutionPath.PATH_A.value, ExecutionPath.MICRO.value],
            "enforcement_flags": ["STAND_DOWN_HARD"],
        },
    )
    assert result["risk_profile"] == "ZERO"
    assert "STAND_DOWN_ZERO_RISK" in result["risk_flags"]


def test_risk_confidence_weighted_calculation():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM", confidence=80),
        regime_row={"regime_classification": MarketRegime.TREND.value, "regime_confidence": 90},
        weighting_advice={"bias_confidence": 70},
        session_review={"session_risk_score": 20, "session_stability_score": 85, "session_quality_score": 80},
        adaptive_thresholds={
            "threshold_adjustments": {"SOFT_BLOCK_THRESHOLD": 60.0, "HARD_BLOCK_THRESHOLD": 85.0},
            "adjustment_flags": ["HIGH_QUALITY_SESSION"],
        },
    )
    assert 0 <= result["risk_confidence"] <= 100
    assert result["risk_confidence"] >= 60


def test_contributing_factors_populated():
    result = decide_epic_risk_envelope(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        regime_row={"regime_classification": MarketRegime.CHOP.value, "regime_confidence": 70},
        session_review={"session_risk_score": 45, "session_quality_score": 70, "session_stability_score": 75},
        hard_row={"active": False},
    )
    factors = result["contributing_factors"]
    assert factors["regime"] == MarketRegime.CHOP.value
    assert factors["strategy"] == "SCALP"
    assert factors["enforcement_state"] == "idle"


def test_build_envelope_for_multiple_epics():
    rows = build_regime_risk_envelope(
        trade_pipeline_health=[{"epic": "CS.D.EURUSD.CFD.IP"}, {"epic": "CS.D.CFPGOLD.CFP.IP"}],
        regime_aware_strategy_selector=[
            _selector("SCALP"),
            {"epic": "CS.D.CFPGOLD.CFP.IP", "recommended_profile": "SWING", "selector_confidence": 65},
        ],
    )
    assert len(rows) == 2


def test_gui_status_includes_regime_risk_envelope(tmp_path, monkeypatch):
    scope = "ig:RRE1"
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
        return_value=[],
    ), patch(
        "api.gui_status.build_api_feed_health",
        return_value={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
    ), patch(
        "api.gui_status.build_market_rotation_status",
        return_value={"active_markets": []},
    ), patch(
        "api.gui_status.build_session_review_bundle",
        return_value={"session_review": {"session_risk_score": 30, "session_stability_score": 70}, "loosening_advice": {}, "self_reflection": {}},
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
            "regime_strategy_alignment": [{"epic": "CS.D.EURUSD.CFD.IP", "recommended_profile": "MOMENTUM"}],
        },
    ), patch(
        "api.gui_status.build_regime_aware_strategy_selector",
        return_value=[_selector("MOMENTUM")],
    ):
        payload = build_gui_status()

    assert "regime_risk_envelope" in payload
    assert isinstance(payload["regime_risk_envelope"], list)
    assert payload["regime_risk_envelope"][0]["risk_profile"] in ("TIGHT", "MEDIUM", "WIDE", "STRUCTURAL", "ZERO")


def test_no_execution_side_effects():
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_regime_risk_envelope(
            trade_pipeline_health=[{"epic": "CS.D.EURUSD.CFD.IP"}],
            regime_aware_strategy_selector=[_selector()],
        )
        live_exec.assert_not_called()


def test_override_hook():
    custom = [{"epic": "X", "risk_profile": "ZERO", "risk_confidence": 99}]
    set_regime_risk_envelope_for_tests(custom)
    assert build_regime_risk_envelope(trade_pipeline_health=[{"epic": "X"}]) == custom
