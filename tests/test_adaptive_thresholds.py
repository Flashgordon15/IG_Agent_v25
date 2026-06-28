"""Adaptive threshold engine (Phase 3 self-learning) tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.adaptive_thresholds import (
    BASELINE_THRESHOLDS,
    build_adaptive_thresholds,
    reset_adaptive_thresholds_for_tests,
    set_adaptive_thresholds_for_tests,
)
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_adaptive_thresholds_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _review(**overrides) -> dict:
    base = {
        "session_summary": {"total_trades": 2},
        "session_quality_score": 60,
        "session_risk_score": 30,
        "session_stability_score": 70,
        "session_flags": [],
    }
    base.update(overrides)
    return base


def _reflection(**overrides) -> dict:
    base = {
        "critique_summary": "test",
        "weaknesses": [],
        "contradictions": [],
        "missed_opportunities": [],
        "strategy_misalignments": [],
        "improvement_suggestions": [],
        "reflection_flags": [],
        "reflection_confidence": 60,
    }
    base.update(overrides)
    return base


def test_under_trading_lowers_thresholds():
    result = build_adaptive_thresholds(
        session_review=_review(session_flags=["UNDER_TRADING"]),
    )
    adj = result["threshold_adjustments"]
    assert adj["SCALP_CONFIDENCE_THRESHOLD"] == BASELINE_THRESHOLDS["SCALP_CONFIDENCE_THRESHOLD"] - 5
    assert adj["MOMENTUM_CONFIDENCE_THRESHOLD"] == BASELINE_THRESHOLDS["MOMENTUM_CONFIDENCE_THRESHOLD"] - 5
    assert adj["SOFT_BLOCK_THRESHOLD"] == BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"] - 5
    assert "UNDER_TRADING_ADJUST" in result["adjustment_flags"]


def test_over_blocking_raises_thresholds():
    result = build_adaptive_thresholds(
        session_review=_review(session_flags=["OVER_BLOCKING_AGGRESSIVE"]),
    )
    adj = result["threshold_adjustments"]
    assert adj["HARD_BLOCK_THRESHOLD"] == BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"] + 5
    assert adj["STAND_DOWN_SENSITIVITY"] == BASELINE_THRESHOLDS["STAND_DOWN_SENSITIVITY"] - 5
    assert "OVER_BLOCKING_ADJUST" in result["adjustment_flags"]


def test_high_quality_session_loosens_gates():
    result = build_adaptive_thresholds(
        session_review=_review(session_quality_score=80),
    )
    adj = result["threshold_adjustments"]
    assert adj["VOLATILITY_GATE_LOW"] < BASELINE_THRESHOLDS["VOLATILITY_GATE_LOW"]
    assert adj["VOLATILITY_GATE_HIGH"] > BASELINE_THRESHOLDS["VOLATILITY_GATE_HIGH"]
    assert adj["FEED_HEALTH_GATE"] < BASELINE_THRESHOLDS["FEED_HEALTH_GATE"]
    assert adj["TRANSITION_CONFIDENCE_THRESHOLD"] < BASELINE_THRESHOLDS["TRANSITION_CONFIDENCE_THRESHOLD"]
    assert "HIGH_QUALITY_SESSION" in result["adjustment_flags"]


def test_high_risk_session_tightens_gates():
    result = build_adaptive_thresholds(
        session_review=_review(session_risk_score=65),
    )
    adj = result["threshold_adjustments"]
    assert adj["VOLATILITY_GATE_LOW"] > BASELINE_THRESHOLDS["VOLATILITY_GATE_LOW"]
    assert adj["VOLATILITY_GATE_HIGH"] < BASELINE_THRESHOLDS["VOLATILITY_GATE_HIGH"]
    assert adj["FEED_HEALTH_GATE"] > BASELINE_THRESHOLDS["FEED_HEALTH_GATE"]
    assert adj["STAND_DOWN_SENSITIVITY"] > BASELINE_THRESHOLDS["STAND_DOWN_SENSITIVITY"]
    assert "HIGH_RISK_SESSION" in result["adjustment_flags"]


def test_strategy_misalignment_adjusts_selector_and_enforcement():
    result = build_adaptive_thresholds(
        session_review=_review(),
        self_reflection=_reflection(reflection_flags=["SELECTOR_ENFORCEMENT_CONFLICT"]),
    )
    adj = result["threshold_adjustments"]
    assert adj["SCALP_CONFIDENCE_THRESHOLD"] > BASELINE_THRESHOLDS["SCALP_CONFIDENCE_THRESHOLD"]
    assert adj["SOFT_BLOCK_THRESHOLD"] < BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"]
    assert adj["HARD_BLOCK_THRESHOLD"] < BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"]
    assert "STRATEGY_ALIGNMENT_ADJUST" in result["adjustment_flags"]


def test_missed_opportunities_lower_confidence_thresholds():
    result = build_adaptive_thresholds(
        session_review=_review(),
        self_reflection=_reflection(reflection_flags=["MISSED_PNL_OPPORTUNITY"]),
    )
    adj = result["threshold_adjustments"]
    assert adj["SCALP_CONFIDENCE_THRESHOLD"] == BASELINE_THRESHOLDS["SCALP_CONFIDENCE_THRESHOLD"] - 5
    assert adj["MOMENTUM_CONFIDENCE_THRESHOLD"] == BASELINE_THRESHOLDS["MOMENTUM_CONFIDENCE_THRESHOLD"] - 5
    assert "MISSED_OPPORTUNITY_ADJUST" in result["adjustment_flags"]


def test_drawdown_increases_protection():
    result = build_adaptive_thresholds(
        session_review=_review(session_flags=["DRAWDOWN_HIGH"]),
    )
    adj = result["threshold_adjustments"]
    assert adj["STAND_DOWN_SENSITIVITY"] > BASELINE_THRESHOLDS["STAND_DOWN_SENSITIVITY"]
    assert adj["HARD_BLOCK_THRESHOLD"] > BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"]
    assert "DRAWDOWN_PROTECTION" in result["adjustment_flags"]


def test_baseline_when_no_signals():
    result = build_adaptive_thresholds(session_review=_review())
    assert result["threshold_adjustments"] == BASELINE_THRESHOLDS
    assert result["adjustment_flags"] == []
    assert "baseline thresholds" in result["adjustment_reason"]


def test_thresholds_clamped_to_bounds():
    result = build_adaptive_thresholds(
        session_review=_review(
            session_flags=["UNDER_TRADING", "DRAWDOWN_HIGH"],
            session_quality_score=95,
            session_risk_score=90,
        ),
        self_reflection=_reflection(
            reflection_flags=["SELECTOR_ENFORCEMENT_CONFLICT", "MISSED_PNL_OPPORTUNITY"]
        ),
    )
    adj = result["threshold_adjustments"]
    for key, (lo, hi) in {
        "SCALP_CONFIDENCE_THRESHOLD": (40.0, 90.0),
        "HARD_BLOCK_THRESHOLD": (75.0, 95.0),
        "VOLATILITY_GATE_LOW": (-3.0, 0.0),
    }.items():
        assert lo <= adj[key] <= hi


def test_gui_status_includes_adaptive_thresholds(tmp_path, monkeypatch):
    scope = "ig:ADAPT1"
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
            "loosening_advice": {"confidence": 50, "loosening_flags": []},
            "self_reflection": _reflection(),
        },
    ):
        payload = build_gui_status()

    assert "adaptive_thresholds" in payload
    adaptive = payload["adaptive_thresholds"]
    assert "threshold_adjustments" in adaptive
    assert "adjustment_reason" in adaptive
    assert "adjustment_flags" in adaptive
    assert "adjustment_confidence" in adaptive
    assert set(adaptive["threshold_adjustments"]) == set(BASELINE_THRESHOLDS)


def test_test_override_hook():
    custom = {
        "threshold_adjustments": {"SCALP_CONFIDENCE_THRESHOLD": 42.0},
        "adjustment_reason": "test",
        "adjustment_flags": ["TEST"],
        "adjustment_confidence": 99,
    }
    set_adaptive_thresholds_for_tests(custom)
    assert build_adaptive_thresholds() == custom
