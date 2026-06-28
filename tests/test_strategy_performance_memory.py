"""Strategy performance memory (Phase 4 v34) tests — advisory only."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_performance_memory import (
    _apply_session_observation,
    _empty_memory,
    _feed_regime,
    _time_of_day_bucket,
    _volatility_regime,
    build_strategy_performance_bundle,
    build_strategy_performance_summary,
    build_strategy_weighting_advice,
    reset_strategy_performance_memory_for_tests,
    set_strategy_performance_memory_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_strategy_performance_memory_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _review(**overrides) -> dict:
    base = {
        "session_summary": {
            "total_trades": 5,
            "trades_by_strategy_profile": {
                "SCALP": 2,
                "MOMENTUM": 2,
                "SWING": 0,
                "ROTATION": 1,
                "STAND_DOWN": 0,
            },
            "points_summary": {
                "closed_wins": 3,
                "closed_losses": 2,
                "closed_pnl_gbp": 12.5,
                "closed_trade_count": 5,
            },
            "volatility_summary": {"mean_z": 1.8, "max_z": 2.1, "spike": True},
            "feed_health_summary": {"overall": "OK"},
            "drawdown_summary": {
                "max_drawdown_pct": 4.0,
                "current_drawdown_pct": 1.5,
            },
            "time_in_profile": {
                "SCALP": 100.0,
                "MOMENTUM": 200.0,
                "SWING": 50.0,
                "ROTATION": 30.0,
                "STAND_DOWN": 10.0,
            },
        },
        "session_flags": [],
    }
    if "session_summary" in overrides:
        merged = {**base["session_summary"], **overrides.pop("session_summary")}
        base["session_summary"] = merged
    base.update(overrides)
    return base


def test_win_rate_updates_correctly():
    memory = _empty_memory()
    _apply_session_observation(memory, session_review=_review())
    assert memory["scalp_win_rate"] != 50.0
    assert memory["momentum_win_rate"] != 50.0
    assert memory["observation_count"] == 1


def test_volatility_regime_performance_tracked():
    memory = _empty_memory()
    _apply_session_observation(memory, session_review=_review())
    high = memory["per_volatility_regime_performance"]["high"]
    assert "SCALP" in high
    assert high["SCALP"] != 50.0


def test_feed_health_regime_performance_tracked():
    memory = _empty_memory()
    review = _review()
    review["session_summary"]["feed_health_summary"] = {"overall": "DEGRADED"}
    _apply_session_observation(
        memory,
        session_review=review,
        api_feed_health={"feeds": {"f1": {"status": "DEGRADED"}}},
    )
    degraded = memory["per_feed_health_regime_performance"]["degraded"]
    assert degraded["ROTATION"] != 50.0


def test_time_of_day_performance_tracked():
    memory = _empty_memory()
    london_now = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
    _apply_session_observation(memory, session_review=_review(), now=london_now)
    london = memory["per_time_of_day_performance"]["london"]
    assert london["MOMENTUM"] != 50.0
    assert _time_of_day_bucket(london_now) == "london"


def test_missed_opportunities_tracked():
    memory = _empty_memory()
    reflection = {"reflection_flags": ["MISSED_PNL_OPPORTUNITY"]}
    _apply_session_observation(memory, session_review=_review(), self_reflection=reflection)
    stats = memory["missed_opportunity_stats"]
    assert stats["missed_opportunity_count"] == 1
    assert stats["missed_opportunity_score"] > 0


def test_drawdown_recovery_tracked():
    memory = _empty_memory()
    _apply_session_observation(memory, session_review=_review())
    recovery = memory["drawdown_recovery_stats"]["MOMENTUM"]
    assert recovery["recovery_count"] >= 1
    assert recovery["last_drawdown_pct"] == 4.0


def test_weighting_advice_scalp_in_high_vol():
    summary = {
        "win_rates": {"scalp_win_rate": 62.0, "momentum_win_rate": 50.0, "swing_win_rate": 50.0, "rotation_win_rate": 50.0},
        "regime_performance": {
            "volatility": {"high": {"SCALP": 65.0, "MOMENTUM": 48.0, "SWING": 50.0, "ROTATION": 50.0}},
            "feed_health": {},
        },
    }
    review = _review()
    review["session_summary"]["volatility_summary"] = {"mean_z": 2.0}
    advice = build_strategy_weighting_advice(performance_summary=summary, session_review=review)
    assert advice["recommended_bias"] == "SCALP"
    assert "SCALP_STRONG_IN_HIGH_VOL" in advice["bias_flags"]


def test_weighting_advice_momentum_in_medium_vol():
    summary = {
        "win_rates": {"scalp_win_rate": 50.0, "momentum_win_rate": 60.0, "swing_win_rate": 50.0, "rotation_win_rate": 50.0},
        "regime_performance": {
            "volatility": {"medium": {"MOMENTUM": 58.0, "SCALP": 50.0, "SWING": 50.0, "ROTATION": 50.0}},
            "feed_health": {},
        },
    }
    review = _review()
    review["session_summary"]["volatility_summary"] = {"mean_z": 1.0}
    advice = build_strategy_weighting_advice(performance_summary=summary, session_review=review)
    assert advice["recommended_bias"] == "MOMENTUM"


def test_volatility_regime_classification():
    assert _volatility_regime({"mean_z": 0.3}) == "low"
    assert _volatility_regime({"mean_z": 1.0}) == "medium"
    assert _volatility_regime({"mean_z": 2.0}) == "high"
    assert _volatility_regime({"mean_z": 3.0}) == "extreme"


def test_feed_regime_classification():
    assert _feed_regime({"overall": "OK"}, None) == "strong"
    assert _feed_regime({"overall": "DEGRADED"}, None) == "degraded"
    mixed_feed = {"feeds": {"a": {"status": "OK"}, "b": {"status": "DEGRADED"}}}
    assert _feed_regime({"overall": "OK"}, mixed_feed) == "mixed"


def test_performance_bundle_structure():
    bundle = build_strategy_performance_bundle(session_review=_review())
    assert "strategy_performance_memory" in bundle
    assert "strategy_weighting_advice" in bundle
    mem = bundle["strategy_performance_memory"]
    assert "win_rates" in mem
    assert "regime_performance" in mem
    assert "epic_performance" in mem
    assert "time_of_day_performance" in mem
    assert "drawdown_recovery" in mem
    assert "missed_opportunity_summary" in mem
    advice = bundle["strategy_weighting_advice"]
    assert "recommended_bias" in advice
    assert "bias_confidence" in advice


def test_gui_status_includes_performance_memory(tmp_path, monkeypatch):
    scope = "ig:PERF1"
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
        return_value={"rotation_phase": "NEUTRAL"},
    ), patch(
        "api.gui_status.build_session_review_bundle",
        return_value={
            "session_review": _review(),
            "loosening_advice": {"confidence": 50},
            "self_reflection": {"reflection_flags": []},
        },
    ), patch(
        "api.gui_status.build_adaptive_thresholds",
        return_value={"threshold_adjustments": {}, "adjustment_flags": []},
    ):
        payload = build_gui_status()

    assert "strategy_performance_memory" in payload
    assert "strategy_weighting_advice" in payload


def test_test_override_hook():
    custom_summary = {"win_rates": {"scalp_win_rate": 99.0}}
    custom_weighting = {"recommended_bias": "SWING", "bias_confidence": 88}
    set_strategy_performance_memory_for_tests(summary=custom_summary, weighting=custom_weighting)
    assert build_strategy_performance_summary() == custom_summary
    assert build_strategy_weighting_advice() == custom_weighting
