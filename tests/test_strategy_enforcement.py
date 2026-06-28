"""Strategy enforcement (Phase 1 soft) tests — no execution side effects."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath
from runtime.hard_enforcement import (
    reset_hard_enforcement_for_tests,
    set_hard_enforcement_decisions_for_tests,
)
from runtime.strategy_enforcement import (
    decide_epic_enforcement,
    build_strategy_enforcement_decisions,
    reset_strategy_enforcement_for_tests,
    set_strategy_enforcement_decisions_for_tests,
    soft_guard_micro_dispatch,
    soft_guard_path_a_execution,
    soft_guard_path_b_handoff,
)
from runtime.unified_execution import reset_unified_execution_for_tests


def _feed_ok() -> dict:
    return {
        "feeds": {
            "feed1": {"status": "OK", "latency_ms": 1000.0},
        },
        "ranking": {"primary": "feed1"},
    }


def _feed_degraded() -> dict:
    return {
        "feeds": {
            "feed1": {"status": "DEGRADED"},
            "feed2": {"status": "DEGRADED"},
        },
        "ranking": {"primary": "feed1"},
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_strategy_enforcement_for_tests()
    reset_hard_enforcement_for_tests()
    reset_unified_execution_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def test_scalp_soft_blocks_path_a_allows_micro():
    decision = decide_epic_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={"ownership": "SCALP", "confidence": 75},
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "SCALP", "confidence": 75},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert ExecutionPath.PATH_A.value in decision.soft_block_paths
    assert ExecutionPath.MICRO.value in decision.soft_allow_paths
    assert ExecutionPath.PATH_B_HANDOFF.value in decision.soft_allow_paths
    assert "SCALP_SOFT_ENFORCEMENT" in decision.enforcement_flags


def test_momentum_soft_blocks_micro_allows_path_a():
    decision = decide_epic_enforcement(
        "IX.D.DOW.IFM.IP",
        controller_row={"ownership": "MOMENTUM", "confidence": 80},
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "MOMENTUM", "confidence": 80},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert ExecutionPath.PATH_A.value in decision.soft_allow_paths
    assert ExecutionPath.MICRO.value in decision.soft_block_paths
    assert ExecutionPath.PATH_B_HANDOFF.value in decision.soft_block_paths
    assert "MOMENTUM_SOFT_ENFORCEMENT" in decision.enforcement_flags


def test_rotation_soft_blocks_execution_allows_sweep():
    decision = decide_epic_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={"ownership": "ROTATION", "confidence": 70},
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "ROTATION", "confidence": 60},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert ExecutionPath.PATH_B_HANDOFF.value in decision.soft_allow_paths
    assert ExecutionPath.PATH_A.value in decision.soft_block_paths
    assert ExecutionPath.MICRO.value in decision.soft_block_paths


def test_rotation_allows_micro_when_selector_scalp_high():
    decision = decide_epic_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={"ownership": "ROTATION", "confidence": 70},
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "SCALP", "confidence": 85},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert ExecutionPath.MICRO.value not in decision.soft_block_paths
    assert ExecutionPath.MICRO.value in decision.soft_allow_paths
    assert "ROTATION_SCALP_EXCEPTION" in decision.enforcement_flags


def test_stand_down_soft_blocks_all_paths():
    decision = decide_epic_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={"ownership": "STAND_DOWN", "confidence": 95},
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "STAND_DOWN", "confidence": 95},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert set(decision.soft_block_paths) == {
        ExecutionPath.PATH_A.value,
        ExecutionPath.MICRO.value,
        ExecutionPath.PATH_B_HANDOFF.value,
    }
    assert "STAND_DOWN_ACTIVE" in decision.enforcement_flags


def test_high_confidence_transition_soft_blocks_current_profile():
    decision = decide_epic_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={"ownership": "SCALP", "confidence": 75},
        transition_row={
            "current_profile": "SCALP",
            "target_profile": "MOMENTUM",
            "transition_confidence": 85,
        },
        selector_advice={"recommended_strategy_profile": "MOMENTUM", "confidence": 85},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert "HIGH_CONFIDENCE_TRANSITION" in decision.enforcement_flags
    assert ExecutionPath.MICRO.value in decision.soft_block_paths
    assert ExecutionPath.PATH_A.value in decision.soft_allow_paths
    assert ExecutionPath.PATH_A.value not in decision.soft_block_paths


def test_soft_guards_respect_injected_decisions():
    set_strategy_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "soft_block_paths": ["PATH_A"],
                "soft_allow_paths": ["MICRO"],
                "enforcement_confidence": 80,
                "enforcement_reason": "test",
                "enforcement_flags": ["SCALP_SOFT_ENFORCEMENT"],
            }
        ]
    )
    assert soft_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
    assert soft_guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is True


def test_guards_never_call_live_executor_or_place_market_order():
    set_strategy_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "soft_block_paths": ["PATH_A", "MICRO", "PATH_B_HANDOFF"],
                "soft_allow_paths": [],
                "enforcement_confidence": 100,
                "enforcement_reason": "STAND_DOWN",
                "enforcement_flags": ["STAND_DOWN_ACTIVE"],
            }
        ]
    )
    live_executor_cls = MagicMock()
    rest_client = MagicMock()

    with patch.dict(
        "sys.modules",
        {"execution.live_executor": MagicMock(LiveExecutor=live_executor_cls)},
    ):
        assert soft_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
        assert soft_guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is False
        assert soft_guard_path_b_handoff("CS.D.EURUSD.CFD.IP") is False

    live_executor_cls.assert_not_called()
    rest_client.place_market_order.assert_not_called()


def test_execution_engine_soft_guard_returns_without_live_executor():
    from data.models import Quote
    from execution.execution_engine import ExecutionEngine
    from execution.types import ExecutionMode, ExecutionResult, TradeSignal

    set_strategy_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "soft_block_paths": ["PATH_A"],
                "soft_allow_paths": ["MICRO"],
                "enforcement_confidence": 80,
                "enforcement_reason": "SCALP soft block",
                "enforcement_flags": [],
            }
        ]
    )
    set_hard_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "active": False,
                "hard_block_paths": [],
                "hard_allow_paths": [],
                "enforcement_confidence": 0,
                "enforcement_reason": "inactive for test",
                "enforcement_flags": [],
            }
        ]
    )
    reset_unified_execution_for_tests()

    engine = ExecutionEngine(mode=ExecutionMode.DEMO, config=MagicMock(), store=MagicMock())
    signal = TradeSignal(
        market="EUR/USD",
        epic="CS.D.EURUSD.CFD.IP",
        direction="BUY",
        raw_confidence=70.0,
        adjusted_confidence=70.0,
        setup_key="test|unit",
        quote=Quote(time=datetime.now(timezone.utc), bid=1.1, offer=1.1002),
    )

    with patch("execution.execution_engine.get_rate_limit_manager"), patch(
        "runtime.strategy_controller.guard_path_a_execution", return_value=True
    ), patch.object(engine, "_live", MagicMock()) as live_mock:
        result = engine.execute_trade(signal, prevalidated=True)

    assert isinstance(result, ExecutionResult)
    assert result.success is False
    assert result.rejection_reason == "soft_blocked_by_strategy_enforcement"
    live_mock.execute.assert_not_called()


def test_gui_status_includes_strategy_enforcement_decisions(tmp_path, monkeypatch):
    scope = "ig:ENF1"
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

    stub_enforcement = [
        {
            "epic": "CS.D.EURUSD.CFD.IP",
            "soft_block_paths": ["PATH_A"],
            "soft_allow_paths": ["MICRO"],
            "enforcement_confidence": 75,
            "enforcement_reason": "SCALP ownership",
            "enforcement_flags": ["SCALP_SOFT_ENFORCEMENT"],
        }
    ]

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
        return_value=stub_enforcement,
    ):
        status = build_gui_status()

    assert "strategy_enforcement_decisions" in status
    assert status["strategy_enforcement_decisions"][0]["soft_block_paths"] == ["PATH_A"]


def test_build_strategy_enforcement_decisions_shape():
    rows = build_strategy_enforcement_decisions(
        trade_pipeline_health=[
            {"epic": "CS.D.EURUSD.CFD.IP", "active_strategy_profile": "SCALP"}
        ],
        pipeline_governance={"per_epic": []},
        api_feed_health=_feed_ok(),
        strategy_controller_decisions=[
            {"epic": "CS.D.EURUSD.CFD.IP", "ownership": "SCALP", "confidence": 75}
        ],
        strategy_transition_advice=[],
        strategy_selector_advice=[],
    )
    assert len(rows) == 1
    item = rows[0]
    assert item["epic"] == "CS.D.EURUSD.CFD.IP"
    assert isinstance(item["soft_block_paths"], list)
    assert isinstance(item["soft_allow_paths"], list)
    assert 0 <= item["enforcement_confidence"] <= 100
    assert isinstance(item["enforcement_reason"], str)
    assert isinstance(item["enforcement_flags"], list)


def test_feed_degraded_flag():
    decision = decide_epic_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={"ownership": "MOMENTUM", "confidence": 80},
        transition_row=None,
        selector_advice=None,
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_degraded(),
    )
    assert "FEED_DEGRADED" in decision.enforcement_flags
