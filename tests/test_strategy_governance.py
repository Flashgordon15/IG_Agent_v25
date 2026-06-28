"""Autonomous strategy governance (Phase 11 v41) tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.regime_detection import MarketRegime
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_governance import (
    build_strategy_governance,
    reset_strategy_governance_for_tests,
    set_strategy_governance_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_strategy_governance_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _perf(**win_rates) -> dict:
    base = {f"{p.lower()}_win_rate": 50.0 for p in ("SCALP", "MOMENTUM", "SWING", "ROTATION")}
    base.update(win_rates)
    return {"win_rates": base, "observation_count": 5}


def _review(stability: int = 70, risk: int = 30, dd_pct: float = 1.0) -> dict:
    return {
        "session_stability_score": stability,
        "session_risk_score": risk,
        "session_quality_score": 70,
        "session_summary": {"drawdown_summary": {"max_drawdown_pct": dd_pct}},
    }


def _momentum_blocks_scalp() -> dict:
    """Active MOMENTUM hard enforcement blocks SCALP (MICRO) and ROTATION paths."""
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "active": True,
        "ownership": "MOMENTUM",
        "hard_block_paths": ["MICRO", "PATH_B_HANDOFF"],
        "hard_allow_paths": ["PATH_A"],
        "enforcement_flags": ["MOMENTUM_HARD_ENFORCEMENT"],
        "enforcement_confidence": 80,
        "enforcement_reason": "MOMENTUM ownership — Path A only",
    }


def test_long_term_scalp_bias():
    result = build_strategy_governance(
        strategy_performance_memory=_perf(scalp_win_rate=65.0, momentum_win_rate=50.0),
        session_review=_review(),
    )
    assert "LONG_TERM_SCALP_BIAS" in result["governance_flags"]
    assert result["governance_adjustments"]["strategy_bias_adjustments"]["SCALP"] > 0
    assert "SCALP_CONFIDENCE_THRESHOLD" in result["governance_adjustments"]["threshold_adjustments"]


def test_long_term_momentum_bias():
    result = build_strategy_governance(
        strategy_performance_memory=_perf(momentum_win_rate=62.0, swing_win_rate=48.0),
        session_review=_review(),
    )
    assert "LONG_TERM_MOMENTUM_BIAS" in result["governance_flags"]
    assert result["governance_adjustments"]["strategy_bias_adjustments"]["MOMENTUM"] > 0


def test_long_term_swing_bias():
    result = build_strategy_governance(
        strategy_performance_memory=_perf(swing_win_rate=64.0),
        session_review=_review(),
    )
    assert "LONG_TERM_SWING_BIAS" in result["governance_flags"]


def test_long_term_rotation_bias():
    result = build_strategy_governance(
        strategy_performance_memory=_perf(rotation_win_rate=61.0),
        session_review=_review(),
    )
    assert "LONG_TERM_ROTATION_BIAS" in result["governance_flags"]


def test_regime_persistence_trend_increases_momentum_decreases_scalp():
    regime_rows = [
        {"epic": "CS.D.EURUSD.CFD.IP", "regime_classification": MarketRegime.TREND.value},
        {"epic": "CS.D.CFPGOLD.CFP.IP", "regime_classification": MarketRegime.TREND.value},
    ]
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            regime_detection=regime_rows,
            session_review=_review(),
        )
    assert "REGIME_PERSISTENCE_TREND" in result["governance_flags"]
    bias = result["governance_adjustments"]["strategy_bias_adjustments"]
    assert bias["MOMENTUM"] > 0
    assert bias["SCALP"] < 0


def test_regime_persistence_chop_increases_scalp_decreases_momentum():
    regime_rows = [{"epic": "CS.D.EURUSD.CFD.IP", "regime_classification": MarketRegime.CHOP.value}]
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            regime_detection=regime_rows,
            session_review=_review(),
        )
    assert "REGIME_PERSISTENCE_CHOP" in result["governance_flags"]
    bias = result["governance_adjustments"]["strategy_bias_adjustments"]
    assert bias["SCALP"] > 0
    assert bias["MOMENTUM"] < 0


def test_regime_persistence_reversal_increases_rotation_decreases_swing():
    regime_rows = [{"epic": "X", "regime_classification": MarketRegime.REVERSAL.value}]
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            regime_detection=regime_rows,
            session_review=_review(),
        )
    assert "REGIME_PERSISTENCE_REVERSAL" in result["governance_flags"]
    bias = result["governance_adjustments"]["strategy_bias_adjustments"]
    assert bias["ROTATION"] > 0
    assert bias["SWING"] < 0


def test_regime_persistence_low_vol_increases_swing_decreases_scalp():
    regime_rows = [{"epic": "X", "regime_classification": MarketRegime.LOW_VOL.value}]
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            regime_detection=regime_rows,
            session_review=_review(),
        )
    assert "REGIME_PERSISTENCE_LOW_VOL" in result["governance_flags"]
    bias = result["governance_adjustments"]["strategy_bias_adjustments"]
    assert bias["SWING"] > 0
    assert bias["SCALP"] < 0


def test_drawdown_cycle_protection_raises_thresholds():
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            session_review=_review(dd_pct=6.0),
        )
    assert "DRAWDOWN_CYCLE_PROTECTION" in result["governance_flags"]
    adj = result["governance_adjustments"]
    assert adj["stand_down_sensitivity_adjustments"] > 0
    assert adj["risk_bias_adjustments"]["tighten"] > 0
    assert adj["sizing_bias_adjustments"]["decrease"] > 0
    assert "SOFT_BLOCK_THRESHOLD" in adj["threshold_adjustments"]
    assert adj["threshold_adjustments"]["SOFT_BLOCK_THRESHOLD"] >= 2.0


def test_daily_target_history_behind():
    for _ in range(4):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            daily_pnl_targeting={"progress_ratio": 0.15},
            session_review=_review(),
        )
    assert "TARGET_HISTORY_BEHIND" in result["governance_flags"]
    adj = result["governance_adjustments"]
    assert adj["sizing_bias_adjustments"]["increase"] > 0
    assert adj["risk_bias_adjustments"]["loosen"] > 0
    assert adj["stand_down_sensitivity_adjustments"] < 0


def test_daily_target_history_ahead():
    for _ in range(4):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            daily_pnl_targeting={"progress_ratio": 0.85},
            session_review=_review(),
        )
    assert "TARGET_HISTORY_AHEAD" in result["governance_flags"]
    adj = result["governance_adjustments"]
    assert adj["sizing_bias_adjustments"]["decrease"] > 0
    assert adj["risk_bias_adjustments"]["tighten"] > 0
    assert adj["stand_down_sensitivity_adjustments"] > 0


def test_enforcement_conflict_history_reduces_blocked_profile_bias():
    hard = [_momentum_blocks_scalp(), dict(_momentum_blocks_scalp(), epic="CS.D.CFPGOLD.CFP.IP")]
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            hard_enforcement_decisions=hard,
            session_review=_review(),
        )
    assert "ENFORCEMENT_CONFLICT_HISTORY" in result["governance_flags"]
    bias = result["governance_adjustments"]["strategy_bias_adjustments"]
    assert bias["SCALP"] < 0
    assert "SCALP_CONFIDENCE_THRESHOLD" in result["governance_adjustments"]["threshold_adjustments"]


def test_session_instability_tighten():
    for _ in range(3):
        result = build_strategy_governance(
            strategy_performance_memory=_perf(),
            daily_pnl_targeting={"progress_ratio": 0.5},
            session_review=_review(stability=45, risk=65),
        )
    assert "SESSION_INSTABILITY_TIGHTEN" in result["governance_flags"]
    adj = result["governance_adjustments"]
    assert adj["threshold_adjustments"].get("SOFT_BLOCK_THRESHOLD", 0) >= 2.0
    assert adj["risk_bias_adjustments"]["tighten"] > 0
    assert adj["sizing_bias_adjustments"]["decrease"] > 0
    assert adj["stand_down_sensitivity_adjustments"] > 0


def test_contributing_factors_populated():
    result = build_strategy_governance(
        strategy_performance_memory=_perf(momentum_win_rate=60.0),
        session_review=_review(),
        daily_pnl_targeting={"progress_ratio": 0.5},
    )
    factors = result["contributing_factors"]
    assert "long_term_performance" in factors
    assert "regime_persistence" in factors
    assert "drawdown_cycles" in factors
    assert "daily_target_history" in factors
    assert "session_stability" in factors
    assert "governance_confidence_components" in factors


def test_governance_confidence_weighted_components():
    result = build_strategy_governance(
        strategy_performance_memory=_perf(momentum_win_rate=70.0),
        session_review=_review(stability=85, risk=20, dd_pct=0.5),
        daily_pnl_targeting={"progress_ratio": 0.5},
    )
    assert 0 <= result["governance_confidence"] <= 100
    components = result["contributing_factors"]["governance_confidence_components"]
    assert "long_term_performance_confidence" in components
    assert "regime_persistence_confidence" in components
    assert "drawdown_cycle_confidence" in components
    assert "daily_target_history_confidence" in components
    assert "enforcement_history_confidence" in components
    expected = int(
        round(
            0.35 * components["long_term_performance_confidence"]
            + 0.25 * components["regime_persistence_confidence"]
            + 0.20 * components["drawdown_cycle_confidence"]
            + 0.10 * components["daily_target_history_confidence"]
            + 0.10 * components["enforcement_history_confidence"]
        )
    )
    assert result["governance_confidence"] == max(0, min(100, expected))


def test_gui_status_includes_strategy_governance(tmp_path, monkeypatch):
    scope = "ig:GOV1"
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

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[]), patch(
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
        return_value={"session_review": _review(), "loosening_advice": {}, "self_reflection": {}},
    ), patch(
        "api.gui_status.build_adaptive_thresholds",
        return_value={"threshold_adjustments": {}, "adjustment_flags": []},
    ), patch(
        "api.gui_status.build_strategy_performance_bundle",
        return_value={"strategy_performance_memory": _perf(), "strategy_weighting_advice": {}},
    ), patch(
        "api.gui_status.build_regime_detection_bundle",
        return_value={"regime_detection": [], "regime_strategy_alignment": []},
    ), patch(
        "api.gui_status.build_regime_aware_strategy_selector",
        return_value=[{"epic": "X", "recommended_profile": "MOMENTUM", "selector_confidence": 70}],
    ), patch(
        "api.gui_status.build_regime_risk_envelope",
        return_value=[],
    ), patch(
        "api.gui_status.build_regime_sizing_advice",
        return_value=[],
    ), patch(
        "api.gui_status.build_daily_pnl_targeting",
        return_value={"progress_ratio": 0.5, "contributing_factors": {}},
    ), patch(
        "api.gui_status.build_unified_execution_routes",
        return_value=[],
    ):
        payload = build_gui_status()

    assert "strategy_governance" in payload
    assert "governance_adjustments" in payload["strategy_governance"]
    assert "governance_confidence" in payload["strategy_governance"]
    keys = list(payload.keys())
    assert keys.index("strategy_governance") == keys.index("unified_execution_route") + 1


def test_no_execution_side_effects():
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_strategy_governance(strategy_performance_memory=_perf(), session_review=_review())
        live_exec.assert_not_called()


def test_override_hook():
    custom = {"governance_confidence": 99, "governance_flags": ["TEST"]}
    set_strategy_governance_for_tests(custom)
    assert build_strategy_governance() == custom
