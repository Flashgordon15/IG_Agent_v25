"""Advisory-only strategy transition engine tests — no execution side effects."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_transition import (
    TransitionProfile,
    advise_epic_transition,
    build_strategy_transition_advice,
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
        "active_strategy_profile": "UNKNOWN",
        "strategy_source": "NONE",
        "ml_appetite": {"appetite": "NONE", "probability": 0.0, "reason": ""},
        "live_tracking": False,
    }
    base.update(overrides)
    return base


def _governance(clean: bool = True) -> tuple[dict, dict]:
    epic_gov = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "pipeline_health_score": 92 if clean else 30,
        "pipeline_anomalies": [] if clean else ["ORDER_PENDING_TOO_LONG"],
        "feed_anomalies": [],
    }
    session = {
        "overall_session_health_score": 90 if clean else 35,
        "session_anomalies": [],
    }
    return {"per_epic": [epic_gov]}, session


def test_scalp_to_momentum_under_volatility_drop_and_ml_strength():
    row = _epic_row(
        active_strategy_profile="SCALP",
        strategy_source="MICRO",
        ml_appetite={"appetite": "STRONG", "probability": 0.72, "reason": "blend"},
        signal_ingested=True,
        order_prepared=True,
    )
    pipeline_gov, session_gov = _governance()
    selector = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_strategy_profile": "MOMENTUM",
        "confidence": 70,
    }
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    with patch("runtime.strategy_transition._volatility_z", return_value=0.9):
        advice = advise_epic_transition(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
            selector_advice_row=selector,
        )

    assert advice.current_profile == "SCALP"
    assert advice.target_profile == "MOMENTUM"
    assert advice.transition_confidence >= 40
    assert "VOLATILITY_DROP" in advice.transition_flags
    assert "ML_STRENGTHENED" in advice.transition_flags


def test_momentum_to_scalp_under_volatility_spike_and_selector_scalp():
    row = _epic_row(
        active_strategy_profile="MOMENTUM",
        strategy_source="PATH_A",
        unrealised_pnl=12.5,
    )
    pipeline_gov, session_gov = _governance()
    selector = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_strategy_profile": "SCALP",
        "confidence": 75,
    }
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    with patch("runtime.strategy_transition._volatility_z", return_value=2.1), patch(
        "runtime.strategy_transition.epic_z_pierce_active", return_value=True
    ):
        advice = advise_epic_transition(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
            selector_advice_row=selector,
        )

    assert advice.current_profile == "MOMENTUM"
    assert advice.target_profile == "SCALP"
    assert "VOLATILITY_SPIKE" in advice.transition_flags
    assert "SELECTOR_SCALP" in advice.transition_flags


def test_rotation_to_scalp_when_stack_pierce_and_strong_feed():
    row = _epic_row(active_strategy_profile="ROTATION", strategy_source="PATH_B_HANDOFF")
    pipeline_gov, session_gov = _governance()
    selector = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_strategy_profile": "SCALP",
        "confidence": 80,
    }
    rotation = {
        "active_markets": ["CS.D.EURUSD.CFD.IP"],
        "candidate_markets": ["IX.D.DOW.IFM.IP"],
        "rotation_state": "EVALUATING",
    }

    with patch("runtime.strategy_transition.epic_z_pierce_active", return_value=True), patch(
        "runtime.strategy_transition._volatility_z", return_value=1.5
    ):
        advice = advise_epic_transition(
            row,
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status=rotation,
            session_governance=session_gov,
            selector_advice_row=selector,
        )

    assert advice.current_profile == "ROTATION"
    assert advice.target_profile == "SCALP"
    assert "ROTATION_STACK_ACTIVE" in advice.transition_flags
    assert "Z_PIERCE" in advice.transition_flags
    assert "FEED_STRONG" in advice.transition_flags


def test_any_to_stand_down_under_degraded_feed_and_critical_governance():
    row = _epic_row(active_strategy_profile="MOMENTUM", strategy_source="PATH_A")
    pipeline_gov, session_gov = _governance(clean=False)
    selector = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_strategy_profile": "STAND_DOWN",
        "confidence": 85,
    }
    rotation = {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}

    advice = advise_epic_transition(
        row,
        pipeline_governance=pipeline_gov,
        api_feed_health=_feed_health_degraded(),
        market_rotation_status=rotation,
        session_governance=session_gov,
        selector_advice_row=selector,
    )

    assert advice.target_profile == TransitionProfile.STAND_DOWN.value
    assert "FEED_DEGRADED" in advice.transition_flags
    assert advice.transition_confidence >= 50


def test_gui_status_includes_strategy_transition_advice(tmp_path, monkeypatch):
    scope = "ig:TRANS1"
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

    stub_row = _epic_row(active_strategy_profile="SCALP", strategy_source="MICRO")
    stub_gov = {
        "pipeline_governance": {"per_epic": []},
        "session_governance": {"overall_session_health_score": 90, "session_anomalies": []},
        "gui_alerts": [],
    }
    stub_transition = [
        {
            "epic": "CS.D.EURUSD.CFD.IP",
            "current_profile": "SCALP",
            "target_profile": "MOMENTUM",
            "transition_confidence": 65,
            "transition_reason": "test",
            "transition_flags": ["VOLATILITY_DROP"],
        }
    ]

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[stub_row]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value=stub_gov,
    ), patch(
        "api.gui_status.build_strategy_selector_advice",
        return_value=[],
    ), patch(
        "api.gui_status.build_strategy_controller_decisions",
        return_value=[],
    ), patch(
        "api.gui_status.build_strategy_transition_advice",
        return_value=stub_transition,
    ):
        status = build_gui_status()

    assert "strategy_transition_advice" in status
    assert status["strategy_transition_advice"][0]["target_profile"] == "MOMENTUM"


def test_no_execution_behaviour_changes():
    row = _epic_row(active_strategy_profile="MOMENTUM")
    pipeline_gov, session_gov = _governance()
    live_executor = MagicMock()

    with patch("runtime.strategy_transition._volatility_z", return_value=1.0), patch.dict(
        "sys.modules",
        {"execution.live_executor": MagicMock(LiveExecutor=live_executor)},
    ):
        build_strategy_transition_advice(
            trade_pipeline_health=[row],
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status={"active_markets": [], "candidate_markets": []},
            session_governance=session_gov,
            strategy_selector_advice=[],
        )

    live_executor.assert_not_called()


def test_build_strategy_transition_advice_shape():
    row = _epic_row(active_strategy_profile="SCALP", strategy_source="MICRO")
    pipeline_gov, session_gov = _governance()

    with patch("runtime.strategy_transition._volatility_z", return_value=1.0):
        advice = build_strategy_transition_advice(
            trade_pipeline_health=[row],
            pipeline_governance=pipeline_gov,
            api_feed_health=_feed_health_ok(),
            market_rotation_status={"active_markets": [], "candidate_markets": []},
            session_governance=session_gov,
            strategy_selector_advice=[],
        )

    assert len(advice) == 1
    item = advice[0]
    assert item["epic"] == "CS.D.EURUSD.CFD.IP"
    assert item["current_profile"] in {
        p.value for p in TransitionProfile
    } | {"UNKNOWN"}
    assert item["target_profile"] in {
        p.value for p in TransitionProfile
    } | {"UNKNOWN"}
    assert 0 <= item["transition_confidence"] <= 100
    assert isinstance(item["transition_reason"], str)
    assert isinstance(item["transition_flags"], list)
