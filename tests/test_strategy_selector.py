"""Advisory-only strategy selector tests — no execution side effects."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_selector import (
    RecommendedStrategyProfile,
    advise_epic,
    build_strategy_selector_advice,
)


def _ago_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _feed_health_ok() -> dict:
    return {
        "feeds": {
            "feed1": {
                "status": "OK",
                "latency_ms": 1200.0,
                "last_update_timestamp": _ago_iso(5),
            },
            "feed2": {"status": "OK", "latency_ms": 2000.0, "last_update_timestamp": _ago_iso(10)},
        },
        "ranking": {"primary": "feed1"},
    }


def _feed_health_degraded() -> dict:
    return {
        "feeds": {
            "feed1": {"status": "DEGRADED", "latency_ms": 25000.0, "last_update_timestamp": _ago_iso(600)},
            "feed2": {"status": "DEGRADED", "latency_ms": 30000.0, "last_update_timestamp": _ago_iso(900)},
        },
        "ranking": {"primary": "feed1"},
    }


def _epic_row(**overrides) -> dict:
    base = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "market_name": "EUR/USD",
        "pipeline_state": "IDLE",
        "active_strategy_profile": "UNKNOWN",
        "strategy_source": "NONE",
        "signal_ingested": False,
        "order_prepared": False,
        "order_dispatched": False,
        "live_tracking": False,
        "ml_appetite": {"appetite": "NONE", "probability": 0.0, "reason": ""},
        "trailing_guards": {"active": False},
    }
    base.update(overrides)
    return base


def _governance_bundle(**epic_overrides) -> tuple[dict, dict]:
    epic_gov = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "pipeline_health_score": 90,
        "pipeline_anomalies": [],
        "feed_anomalies": [],
        "rotation_anomalies": [],
    }
    epic_gov.update(epic_overrides)
    pipeline_governance = {"per_epic": [epic_gov]}
    session_governance = {"overall_session_health_score": 90, "session_anomalies": []}
    return pipeline_governance, session_governance


def test_scalp_recommended_when_micro_active_and_feed_strong():
    row = _epic_row(
        pipeline_state="LIVE",
        active_strategy_profile="SCALP",
        strategy_source="MICRO",
        order_dispatched=True,
        order_confirmed=True,
    )
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    with patch("runtime.strategy_selector._volatility_z", return_value=2.0):
        advice = advise_epic(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
        )

    assert advice.recommended_strategy_profile is RecommendedStrategyProfile.SCALP
    assert advice.confidence >= 50
    assert "FEED_STRONG" in advice.advisory_flags or "MICRO_ACTIVE" in advice.advisory_flags


def test_momentum_recommended_when_path_a_lifecycle_and_ml_appetite():
    row = _epic_row(
        pipeline_state="LIVE",
        active_strategy_profile="MOMENTUM",
        strategy_source="PATH_A",
        signal_ingested=True,
        order_prepared=True,
        live_tracking=True,
        ml_appetite={"appetite": "STRONG", "probability": 0.72, "reason": "blend"},
        trailing_guards={"active": True},
    )
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    with patch("runtime.strategy_selector._volatility_z", return_value=0.8):
        advice = advise_epic(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
        )

    assert advice.recommended_strategy_profile is RecommendedStrategyProfile.MOMENTUM
    assert "ML_APPETITE_PRESENT" in advice.advisory_flags
    assert "PATH_A_LIFECYCLE" in advice.advisory_flags


def test_swing_recommended_when_long_duration_path_a_hold():
    row = _epic_row(
        pipeline_state="LIVE",
        active_strategy_profile="SWING",
        strategy_source="PATH_A",
        signal_ingested=True,
        live_tracking=True,
        live_tracking_timestamp=_ago_iso(7200),
        ml_appetite={"appetite": "WEAK", "probability": 0.55, "reason": "blend"},
    )
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    with patch("runtime.strategy_selector._volatility_z", return_value=1.2):
        advice = advise_epic(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
        )

    assert advice.recommended_strategy_profile is RecommendedStrategyProfile.SWING
    assert "LONG_DURATION_HOLD" in advice.advisory_flags


def test_rotation_recommended_when_active_stack_with_z_pierce():
    row = _epic_row(epic="CS.D.EURUSD.CFD.IP")
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {
        "active_markets": ["CS.D.EURUSD.CFD.IP"],
        "candidate_markets": ["IX.D.DOW.IFM.IP"],
        "rotation_state": "EVALUATING",
    }

    with patch("runtime.strategy_selector.epic_z_pierce_active", return_value=True), patch(
        "runtime.strategy_selector._volatility_z", return_value=0.5
    ):
        advice = advise_epic(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
        )

    assert advice.recommended_strategy_profile is RecommendedStrategyProfile.ROTATION
    assert "ACTIVE_STACK" in advice.advisory_flags
    assert "Z_PIERCE" in advice.advisory_flags


def test_stand_down_when_feed_degraded():
    row = _epic_row()
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    advice = advise_epic(
        row,
        pipeline_governance=pipeline_gov,
        api_feed_health=_feed_health_degraded(),
        market_rotation_status=rotation,
        session_governance=session_gov,
    )

    assert advice.recommended_strategy_profile is RecommendedStrategyProfile.STAND_DOWN
    assert "FEED_DEGRADED" in advice.advisory_flags


def test_stand_down_when_governance_critical():
    row = _epic_row(
        pipeline_state="ORDER_PENDING",
        order_dispatched=True,
        active_strategy_profile="SCALP",
        strategy_source="MICRO",
    )
    pipeline_gov, session_gov = _governance_bundle(
        pipeline_anomalies=["ORDER_PENDING_TOO_LONG"],
        pipeline_health_score=25,
    )
    session_gov = {"overall_session_health_score": 35, "session_anomalies": ["MULTIPLE_EPICS_STALLED_IN_ORDER_PENDING"]}
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    advice = advise_epic(
        row,
        pipeline_governance=pipeline_gov,
        api_feed_health=_feed_health_ok(),
        market_rotation_status=rotation,
        session_governance=session_gov,
    )

    assert advice.recommended_strategy_profile is RecommendedStrategyProfile.STAND_DOWN
    assert "PIPELINE_CRITICAL" in advice.advisory_flags or "SESSION_HEALTH_LOW" in advice.advisory_flags


def test_gui_status_includes_strategy_selector_advice(tmp_path, monkeypatch):
    scope = "ig:SEL1"
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

    stub_row = _epic_row(
        pipeline_state="LIVE",
        active_strategy_profile="MOMENTUM",
        strategy_source="PATH_A",
        signal_ingested=True,
        order_prepared=True,
        ml_appetite={"appetite": "WEAK", "probability": 0.5, "reason": ""},
        trailing_guards={"active": True},
    )
    stub_gov = {
        "pipeline_governance": {
            "per_epic": [
                {
                    "epic": "CS.D.EURUSD.CFD.IP",
                    "pipeline_health_score": 95,
                    "pipeline_anomalies": [],
                    "feed_anomalies": [],
                    "rotation_anomalies": [],
                }
            ]
        },
        "session_governance": {"overall_session_health_score": 95, "session_anomalies": []},
        "gui_alerts": [],
    }
    stub_advice = [
        {
            "epic": "CS.D.EURUSD.CFD.IP",
            "recommended_strategy_profile": "MOMENTUM",
            "confidence": 80,
            "reason": "test",
            "expected_horizon": "minutes",
            "expected_risk_envelope": "low",
            "expected_points_target": 12,
            "advisory_flags": ["ML_APPETITE_PRESENT"],
        }
    ]

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[stub_row]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value=stub_gov,
    ), patch("api.gui_status.build_strategy_selector_advice", return_value=stub_advice):
        status = build_gui_status()

    assert "strategy_selector_advice" in status
    assert status["strategy_selector_advice"][0]["recommended_strategy_profile"] == "MOMENTUM"
    assert status["strategy_selector_advice"][0]["confidence"] == 80


def test_build_strategy_selector_advice_shape():
    row = _epic_row(
        pipeline_state="LIVE",
        active_strategy_profile="SCALP",
        strategy_source="MICRO",
    )
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    with patch("runtime.strategy_selector._volatility_z", return_value=2.0):
        advice = build_strategy_selector_advice(
            trade_pipeline_health=[row],
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
        )

    assert len(advice) == 1
    item = advice[0]
    assert item["epic"] == "CS.D.EURUSD.CFD.IP"
    assert item["recommended_strategy_profile"] in {
        p.value for p in RecommendedStrategyProfile
    }
    assert 0 <= item["confidence"] <= 100
    assert isinstance(item["reason"], str)
    assert item["expected_horizon"] in ("seconds", "minutes", "hours")
    assert item["expected_risk_envelope"] in ("low", "medium", "high")
    assert isinstance(item["expected_points_target"], int)
    assert isinstance(item["advisory_flags"], list)


def test_no_execution_behaviour_changes():
    """Strategy selector must not invoke execution or dispatch paths."""
    row = _epic_row(
        pipeline_state="LIVE",
        active_strategy_profile="SCALP",
        strategy_source="MICRO",
    )
    pipeline_gov, session_gov = _governance_bundle()
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    live_executor = MagicMock()
    risk_manager = MagicMock()

    with patch("runtime.strategy_selector._volatility_z", return_value=1.5), patch.dict(
        "sys.modules",
        {
            "execution.live_executor": live_executor,
            "execution.risk_manager": risk_manager,
        },
    ):
        build_strategy_selector_advice(
            trade_pipeline_health=[row],
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
        )

    live_executor.LiveExecutor.assert_not_called()
    risk_manager.RiskManager.assert_not_called()
