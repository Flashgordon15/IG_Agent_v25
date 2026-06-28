"""Regime-aware sizing engine (Phase 8 v38) tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.regime_detection import MarketRegime
from runtime.regime_sizing import (
    build_regime_sizing_advice,
    decide_epic_sizing_advice,
    reset_regime_sizing_for_tests,
    set_regime_sizing_for_tests,
)
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_regime_sizing_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _selector(profile: str = "MOMENTUM", confidence: int = 70) -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_profile": profile,
        "selector_confidence": confidence,
    }


def _risk(profile: str = "MEDIUM", confidence: int = 65) -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "risk_profile": profile,
        "risk_confidence": confidence,
    }


def test_scalp_sizing_rules():
    baseline = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        risk_row=_risk("TIGHT"),
        regime_row={"regime_classification": MarketRegime.CHOP.value, "regime_confidence": 60},
        performance_memory={"win_rates": {"scalp_win_rate": 50.0}},
    )
    boosted = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP", confidence=80),
        risk_row=_risk("TIGHT"),
        regime_row={"regime_classification": MarketRegime.LOW_VOL.value, "regime_confidence": 70},
        performance_memory={"win_rates": {"scalp_win_rate": 65.0}},
    )
    assert boosted["recommended_size_factor"] > baseline["recommended_size_factor"]
    assert "SCALP_SIZING" in boosted["sizing_flags"]


def test_scalp_decreases_under_extreme_vol():
    base = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        risk_row=_risk("TIGHT"),
        regime_row={"regime_classification": MarketRegime.CHOP.value, "regime_confidence": 60},
    )
    reduced = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        risk_row=_risk("TIGHT"),
        regime_row={"regime_classification": MarketRegime.EXTREME_VOL.value, "regime_confidence": 85},
    )
    assert reduced["recommended_size_factor"] < base["recommended_size_factor"]
    assert "SCALP_SIZE_DOWN_EXTREME_VOL" in reduced["sizing_flags"]


def test_momentum_sizing_rules():
    result = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk("MEDIUM"),
        regime_row={"regime_classification": MarketRegime.TREND.value, "regime_confidence": 80},
        performance_memory={"win_rates": {"momentum_win_rate": 62.0}},
    )
    assert result["recommended_size_factor"] >= 0.25
    assert "MOMENTUM_SIZING" in result["sizing_flags"]


def test_momentum_decreases_under_chop():
    result = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk("TIGHT"),
        regime_row={"regime_classification": MarketRegime.CHOP.value, "regime_confidence": 75},
    )
    assert result["recommended_size_factor"] < 0.25
    assert "MOMENTUM_SIZE_DOWN_CHOP" in result["sizing_flags"]


def test_swing_sizing_rules():
    result = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SWING"),
        risk_row=_risk("WIDE"),
        regime_row={"regime_classification": MarketRegime.LOW_VOL.value, "regime_confidence": 70},
        performance_memory={"win_rates": {"swing_win_rate": 60.0}},
    )
    assert result["recommended_size_factor"] >= 0.40
    assert "SWING_SIZING" in result["sizing_flags"]


def test_rotation_sizing_rules():
    result = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("ROTATION"),
        risk_row=_risk("STRUCTURAL"),
        regime_row={"regime_classification": MarketRegime.REVERSAL.value, "regime_confidence": 75},
        performance_memory={"win_rates": {"rotation_win_rate": 55.0}},
    )
    assert result["recommended_size_factor"] >= 0.30
    assert "ROTATION_SIZING" in result["sizing_flags"]


def test_stand_down_zero_sizing():
    result = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("STAND_DOWN"),
        hard_row={
            "active": True,
            "hard_allow_paths": [],
            "enforcement_flags": ["STAND_DOWN_HARD"],
        },
    )
    assert result["recommended_size_factor"] == 0.0
    assert "STAND_DOWN_ZERO_SIZE" in result["sizing_flags"]


def test_risk_envelope_modifies_sizing():
    tight = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk("TIGHT"),
    )
    wide = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk("WIDE"),
    )
    assert wide["recommended_size_factor"] > tight["recommended_size_factor"]
    assert "RISK_ENVELOPE_SIZING" in wide["sizing_flags"]


def test_performance_memory_modifies_sizing():
    weak = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk(),
        regime_row={"regime_classification": MarketRegime.TREND.value, "regime_confidence": 80},
        performance_memory={"win_rates": {"momentum_win_rate": 45.0}},
    )
    strong = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk(),
        regime_row={"regime_classification": MarketRegime.TREND.value, "regime_confidence": 80},
        performance_memory={"win_rates": {"momentum_win_rate": 65.0}},
    )
    assert strong["recommended_size_factor"] > weak["recommended_size_factor"]


def test_threshold_adjustments_modify_confidence():
    base = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk(),
        adaptive_thresholds={"threshold_adjustments": {}},
    )
    loosened = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk(),
        adaptive_thresholds={
            "threshold_adjustments": {"SOFT_BLOCK_THRESHOLD": 60.0},
            "adjustment_flags": ["UNDER_TRADING_ADJUST"],
        },
    )
    assert loosened["sizing_confidence"] >= base["sizing_confidence"]
    assert "THRESHOLD_CONFIDENCE_ADJUST" in loosened["sizing_flags"]


def test_session_risk_modifies_sizing():
    low_risk = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        risk_row=_risk("TIGHT"),
        session_review={"session_risk_score": 25, "session_quality_score": 70, "session_stability_score": 75},
    )
    high_risk = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("SCALP"),
        risk_row=_risk("TIGHT"),
        session_review={"session_risk_score": 70, "session_quality_score": 70, "session_stability_score": 75},
    )
    assert high_risk["recommended_size_factor"] < low_risk["recommended_size_factor"]
    assert "SESSION_RISK_SIZE_DOWN" in high_risk["sizing_flags"]


def test_contributing_factors_populated():
    result = decide_epic_sizing_advice(
        "CS.D.EURUSD.CFD.IP",
        selector_row=_selector("MOMENTUM"),
        risk_row=_risk(),
        regime_row={"regime_classification": "TREND", "regime_confidence": 75},
        session_review={"session_risk_score": 30, "session_quality_score": 80, "session_stability_score": 85},
    )
    factors = result["contributing_factors"]
    assert factors["regime"] == "TREND"
    assert factors["strategy"] == "MOMENTUM"
    assert factors["risk_envelope"] == "MEDIUM"
    assert "session_state" in factors


def test_gui_status_includes_regime_sizing_advice(tmp_path, monkeypatch):
    scope = "ig:RSZ1"
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
        return_value={"session_review": {"session_risk_score": 30, "session_stability_score": 70, "session_quality_score": 75}, "loosening_advice": {}, "self_reflection": {}},
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
    ), patch(
        "api.gui_status.build_regime_risk_envelope",
        return_value=[_risk("MEDIUM")],
    ):
        payload = build_gui_status()

    assert "regime_sizing_advice" in payload
    assert isinstance(payload["regime_sizing_advice"], list)
    assert 0.0 <= payload["regime_sizing_advice"][0]["recommended_size_factor"] <= 1.0


def test_no_execution_side_effects():
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_regime_sizing_advice(
            trade_pipeline_health=[{"epic": "CS.D.EURUSD.CFD.IP"}],
            regime_aware_strategy_selector=[_selector()],
            regime_risk_envelope=[_risk()],
        )
        live_exec.assert_not_called()


def test_override_hook():
    custom = [{"epic": "X", "recommended_size_factor": 0.5, "sizing_confidence": 88}]
    set_regime_sizing_for_tests(custom)
    assert build_regime_sizing_advice(trade_pipeline_health=[{"epic": "X"}]) == custom
