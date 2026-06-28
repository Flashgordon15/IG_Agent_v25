"""Daily P&L targeting engine (Phase 9 v39) tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.daily_pnl_targeting import (
    DEFAULT_TARGET_POINTS,
    build_daily_pnl_targeting,
    reset_daily_pnl_targeting_for_tests,
    set_daily_pnl_targeting_for_tests,
)
from runtime.regime_detection import MarketRegime
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_daily_pnl_targeting_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB", "DAILY_PNL_TARGET_POINTS"):
        monkeypatch.delenv(key, raising=False)


def _review(points_gbp: float = 0.0, wins: int = 0, **scores) -> dict:
    return {
        "session_summary": {
            "points_summary": {
                "combined_pnl_gbp": points_gbp,
                "closed_pnl_gbp": points_gbp,
                "closed_wins": wins,
            }
        },
        "session_quality_score": scores.get("quality", 70),
        "session_risk_score": scores.get("risk", 30),
        "session_stability_score": scores.get("stability", 75),
    }


def _selector(profile: str = "MOMENTUM", confidence: int = 75) -> dict:
    return {"epic": "CS.D.EURUSD.CFD.IP", "recommended_profile": profile, "selector_confidence": confidence}


def _sizing(factor: float = 0.25, confidence: int = 70) -> dict:
    return {"epic": "CS.D.EURUSD.CFD.IP", "recommended_size_factor": factor, "sizing_confidence": confidence}


def _risk(profile: str = "MEDIUM", confidence: int = 65) -> dict:
    return {"epic": "CS.D.EURUSD.CFD.IP", "risk_profile": profile, "risk_confidence": confidence}


def test_ahead_of_target_protection():
    # 800+ points equivalent: £80 * 10 = 800 on 1000 target
    result = build_daily_pnl_targeting(
        session_review=_review(points_gbp=80.0, wins=2),
        regime_aware_strategy_selector=[_selector()],
        regime_sizing_advice=[_sizing()],
        regime_risk_envelope=[_risk()],
    )
    assert result["progress_ratio"] >= 0.75
    assert "AHEAD_OF_TARGET_PROTECTION" in result["bias_flags"]
    assert result["recommended_bias"]["sizing_bias"] < 0
    assert result["recommended_bias"]["stand_down_bias"] > 0.2


def test_on_track_stable():
    result = build_daily_pnl_targeting(
        session_review=_review(points_gbp=50.0, wins=1),
        regime_aware_strategy_selector=[_selector()],
        regime_sizing_advice=[_sizing()],
        regime_risk_envelope=[_risk()],
        regime_detection=[{"epic": "CS.D.EURUSD.CFD.IP", "regime_classification": MarketRegime.TREND.value}],
    )
    assert 0.40 <= result["progress_ratio"] < 0.75
    assert "ON_TRACK_STABLE" in result["bias_flags"]


def test_behind_target_aggressive():
    result = build_daily_pnl_targeting(
        session_review=_review(points_gbp=25.0),
        regime_aware_strategy_selector=[_selector()],
        regime_sizing_advice=[_sizing()],
        regime_risk_envelope=[_risk()],
    )
    assert 0.20 <= result["progress_ratio"] < 0.40
    assert "BEHIND_TARGET_AGGRESSIVE" in result["bias_flags"]
    assert result["recommended_bias"]["sizing_bias"] > 0
    assert result["recommended_bias"]["frequency_bias"] > 0


def test_far_behind_aggressive():
    result = build_daily_pnl_targeting(
        session_review=_review(points_gbp=5.0),
        regime_aware_strategy_selector=[_selector()],
        regime_sizing_advice=[_sizing()],
        regime_risk_envelope=[_risk()],
    )
    assert result["progress_ratio"] < 0.20
    assert "FAR_BEHIND_AGGRESSIVE" in result["bias_flags"]
    assert result["recommended_bias"]["sizing_bias"] >= 0.25
    assert result["recommended_bias"]["risk_bias"] >= 0.25


def test_target_points_default():
    result = build_daily_pnl_targeting(session_review=_review())
    assert result["target_points"] == DEFAULT_TARGET_POINTS


def test_target_points_env_override(monkeypatch):
    monkeypatch.setenv("DAILY_PNL_TARGET_POINTS", "500")
    result = build_daily_pnl_targeting(session_review=_review())
    assert result["target_points"] == 500


def test_contributing_factors_populated():
    result = build_daily_pnl_targeting(
        session_review=_review(points_gbp=30.0),
        regime_aware_strategy_selector=[_selector("SCALP")],
        regime_sizing_advice=[_sizing(0.15)],
        regime_risk_envelope=[_risk("TIGHT")],
        regime_detection=[{"regime_classification": MarketRegime.CHOP.value}],
        adaptive_thresholds={"adjustment_flags": ["UNDER_TRADING_ADJUST"]},
        hard_enforcement_decisions=[{"active": True}],
    )
    factors = result["contributing_factors"]
    assert "session_progress" in factors
    assert factors["regime"] == MarketRegime.CHOP.value
    assert factors["risk_envelope"] == "TIGHT"
    assert factors["enforcement_state"]["active_epics"] == 1


def test_session_risk_tightens_bias():
    low = build_daily_pnl_targeting(
        session_review=_review(points_gbp=10.0, risk=25),
        regime_aware_strategy_selector=[_selector()],
        regime_sizing_advice=[_sizing()],
        regime_risk_envelope=[_risk()],
    )
    high = build_daily_pnl_targeting(
        session_review=_review(points_gbp=10.0, risk=70),
        regime_aware_strategy_selector=[_selector()],
        regime_sizing_advice=[_sizing()],
        regime_risk_envelope=[_risk()],
    )
    assert high["recommended_bias"]["stand_down_bias"] > low["recommended_bias"]["stand_down_bias"]
    assert "SESSION_RISK_BIAS_TIGHTEN" in high["bias_flags"]


def test_gui_status_includes_daily_pnl_targeting(tmp_path, monkeypatch):
    scope = "ig:PNL1"
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

    review = {
        "session_review": _review(points_gbp=40.0),
        "loosening_advice": {},
        "self_reflection": {},
    }

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
        return_value=[],
    ), patch(
        "api.gui_status.build_api_feed_health",
        return_value={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
    ), patch(
        "api.gui_status.build_market_rotation_status",
        return_value={"active_markets": []},
    ), patch(
        "api.gui_status.build_session_review_bundle",
        return_value=review,
    ), patch(
        "api.gui_status.build_adaptive_thresholds",
        return_value={"threshold_adjustments": {}, "adjustment_flags": []},
    ), patch(
        "api.gui_status.build_strategy_performance_bundle",
        return_value={"strategy_performance_memory": {}, "strategy_weighting_advice": {}},
    ), patch(
        "api.gui_status.build_regime_detection_bundle",
        return_value={"regime_detection": [], "regime_strategy_alignment": []},
    ), patch(
        "api.gui_status.build_regime_aware_strategy_selector",
        return_value=[_selector()],
    ), patch(
        "api.gui_status.build_regime_risk_envelope",
        return_value=[_risk()],
    ), patch(
        "api.gui_status.build_regime_sizing_advice",
        return_value=[_sizing()],
    ):
        payload = build_gui_status()

    assert "daily_pnl_targeting" in payload
    assert "target_points" in payload["daily_pnl_targeting"]
    assert "recommended_bias" in payload["daily_pnl_targeting"]


def test_no_execution_side_effects():
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_daily_pnl_targeting(session_review=_review())
        live_exec.assert_not_called()


def test_override_hook():
    custom = {"target_points": 100, "current_points": 50, "progress_ratio": 0.5}
    set_daily_pnl_targeting_for_tests(custom)
    assert build_daily_pnl_targeting() == custom
