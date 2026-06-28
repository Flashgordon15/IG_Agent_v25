"""Session review, loosening advisor, and self-reflection tests — advisory only."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.session_review import (
    build_loosening_advice,
    build_self_reflection,
    build_session_review,
    build_session_review_bundle,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _base_review(**overrides) -> dict:
    base = {
        "session_summary": {
            "total_trades": 5,
            "drawdown_summary": {"max_drawdown_pct": 1.5},
            "governance_summary": {"session_anomalies": [], "epic_anomaly_count": 0},
            "time_in_profile": {
                "SCALP": 100.0,
                "MOMENTUM": 200.0,
                "SWING": 0.0,
                "ROTATION": 50.0,
                "STAND_DOWN": 10.0,
            },
        },
        "session_quality_score": 75,
        "session_risk_score": 35,
        "session_stability_score": 80,
        "session_flags": [],
    }
    base.update(overrides)
    return base


def test_session_review_structure():
    review = build_session_review(
        trade_pipeline_health=[
            {"epic": "CS.D.EURUSD.CFD.IP", "active_strategy_profile": "SCALP", "strategy_source": "MICRO"}
        ],
        pipeline_governance={"per_epic": []},
        session_governance={"overall_session_health_score": 85, "session_anomalies": []},
        api_feed_health={"feeds": {"feed1": {"status": "OK"}}, "ranking": {}},
        strategy_controller_decisions=[],
        strategy_enforcement_decisions=[],
        session_uptime_sec=3600.0,
    )
    assert "session_summary" in review
    summary = review["session_summary"]
    assert "total_trades" in summary
    assert "trades_by_path" in summary
    assert "soft_blocks_count" in summary
    assert "time_in_profile" in summary
    assert 0 <= review["session_quality_score"] <= 100
    assert 0 <= review["session_risk_score"] <= 100
    assert 0 <= review["session_stability_score"] <= 100
    assert isinstance(review["session_flags"], list)


def test_loosening_advice_high_quality_low_risk():
    advice = build_loosening_advice(_base_review())
    assert advice["confidence"] >= 70
    assert any("frequency" in c.lower() for c in advice["recommended_changes"])
    assert "HIGH_QUALITY_LOW_RISK" in advice["loosening_flags"]


def test_loosening_advice_under_trading():
    review = _base_review(session_flags=["UNDER_TRADING"])
    advice = build_loosening_advice(review)
    assert "UNDER_TRADING" in advice["loosening_flags"]
    assert any("70" in c and "60" in c for c in advice["recommended_changes"])


def test_loosening_advice_over_blocked():
    review = _base_review(session_flags=["OVER_BLOCKED"])
    advice = build_loosening_advice(review)
    assert "OVER_BLOCKED" in advice["loosening_flags"]
    assert any("controller" in c.lower() for c in advice["recommended_changes"])


def test_self_reflection_detects_profile_source_mismatch():
    review = _base_review()
    reflection = build_self_reflection(
        review,
        trade_pipeline_health=[
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "active_strategy_profile": "MOMENTUM",
                "strategy_source": "MICRO",
            }
        ],
    )
    assert len(reflection["contradictions"]) >= 1
    assert len(reflection["strategy_misalignments"]) >= 1


def test_self_reflection_detects_selector_controller_drift():
    review = _base_review()
    reflection = build_self_reflection(
        review,
        strategy_selector_advice=[
            {"epic": "CS.D.EURUSD.CFD.IP", "recommended_strategy_profile": "SCALP", "confidence": 80}
        ],
        strategy_controller_decisions=[
            {"epic": "CS.D.EURUSD.CFD.IP", "ownership": "MOMENTUM", "confidence": 80}
        ],
    )
    assert "SELECTOR_CONTROLLER_DRIFT" in reflection["reflection_flags"]
    assert any("CS.D.EURUSD" in m for m in reflection["strategy_misalignments"])


def test_self_reflection_high_confidence_transition_lag():
    review = _base_review()
    reflection = build_self_reflection(
        review,
        strategy_transition_advice=[
            {
                "epic": "IX.D.DOW.IFM.IP",
                "current_profile": "SCALP",
                "target_profile": "MOMENTUM",
                "transition_confidence": 85,
            }
        ],
    )
    assert "TRANSITION_LAG" in reflection["reflection_flags"]
    assert len(reflection["missed_opportunities"]) >= 1


def test_session_review_flags_feed_degraded():
    review = build_session_review(
        trade_pipeline_health=[],
        pipeline_governance={"per_epic": []},
        session_governance={"overall_session_health_score": 40, "session_anomalies": ["X"]},
        api_feed_health={
            "feeds": {"feed1": {"status": "DEGRADED"}, "feed2": {"status": "DEGRADED"}},
            "ranking": {},
        },
        strategy_controller_decisions=[{"epic": "E1", "blocked_paths": ["PATH_A", "MICRO", "PATH_B_HANDOFF"]}] * 3,
        strategy_enforcement_decisions=[{"epic": "E1", "soft_block_paths": ["PATH_A", "MICRO"]}] * 3,
        session_uptime_sec=7200.0,
    )
    flags = set(review["session_flags"])
    assert "FEED_DEGRADED" in flags or "OVER_BLOCKED" in flags or "UNDER_TRADING" in flags


def test_build_session_review_bundle():
    bundle = build_session_review_bundle(
        trade_pipeline_health=[],
        pipeline_governance={"per_epic": []},
        session_governance={"overall_session_health_score": 90, "session_anomalies": []},
        api_feed_health={"feeds": {"feed1": {"status": "OK"}}, "ranking": {}},
        session_uptime_sec=1800.0,
    )
    assert "session_review" in bundle
    assert "loosening_advice" in bundle
    assert "self_reflection" in bundle


def test_gui_status_includes_session_review_fields(tmp_path, monkeypatch):
    scope = "ig:REV1"
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

    stub_bundle = {
        "session_review": {"session_quality_score": 80, "session_flags": []},
        "loosening_advice": {"recommended_changes": ["none"], "confidence": 50},
        "self_reflection": {"critique_summary": "ok", "weaknesses": []},
    }

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
        "api.gui_status.build_session_review_bundle",
        return_value=stub_bundle,
    ):
        status = build_gui_status()

    assert "session_review" in status
    assert "loosening_advice" in status
    assert "self_reflection" in status
    assert status["session_review"]["session_quality_score"] == 80


def test_no_execution_behaviour_changes():
    live_executor = MagicMock()
    with patch.dict(
        "sys.modules",
        {"execution.live_executor": MagicMock(LiveExecutor=live_executor)},
    ), patch("runtime.session_review._fetch_session_trades", return_value={"total_trades": 0, "trades_by_path": {}, "trades_by_strategy_profile": {}, "closed_pnl_gbp": [], "unrealised_pnl_gbp": []}):
        build_session_review_bundle(
            trade_pipeline_health=[],
            pipeline_governance={"per_epic": []},
            session_governance={},
            api_feed_health={"feeds": {}, "ranking": {}},
        )
    live_executor.assert_not_called()
