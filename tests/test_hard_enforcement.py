"""Hard enforcement (Phase 2 execution binding) tests — no execution side effects."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.hard_enforcement import (
    build_hard_enforcement_decisions,
    decide_epic_hard_enforcement,
    hard_guard_micro_dispatch,
    hard_guard_path_a_execution,
    hard_guard_path_b_handoff,
    is_hard_enforcement_active,
    reset_hard_enforcement_for_tests,
    set_hard_enforcement_decisions_for_tests,
)
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath
from runtime.strategy_enforcement import (
    reset_strategy_enforcement_for_tests,
    set_strategy_enforcement_decisions_for_tests,
)


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


def _controller_scalp() -> dict:
    return {
        "ownership": "SCALP",
        "confidence": 75,
        "blocked_paths": [ExecutionPath.PATH_A.value],
        "allowed_paths": [ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value],
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_hard_enforcement_for_tests()
    reset_strategy_enforcement_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def test_scalp_hard_blocks_path_a_and_path_b_allows_micro():
    decision = decide_epic_hard_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row=_controller_scalp(),
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "SCALP", "confidence": 75},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert decision.active is True
    assert ExecutionPath.PATH_A.value in decision.hard_block_paths
    assert ExecutionPath.PATH_B_HANDOFF.value in decision.hard_block_paths
    assert ExecutionPath.MICRO.value in decision.hard_allow_paths
    assert ExecutionPath.MICRO.value not in decision.hard_block_paths
    assert "SCALP_HARD_ENFORCEMENT" in decision.enforcement_flags


def test_momentum_hard_blocks_micro_allows_path_a():
    decision = decide_epic_hard_enforcement(
        "IX.D.DOW.IFM.IP",
        controller_row={
            "ownership": "MOMENTUM",
            "confidence": 80,
            "blocked_paths": [ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value],
        },
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "MOMENTUM", "confidence": 80},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert decision.active is True
    assert ExecutionPath.PATH_A.value in decision.hard_allow_paths
    assert ExecutionPath.MICRO.value in decision.hard_block_paths
    assert ExecutionPath.PATH_B_HANDOFF.value in decision.hard_block_paths
    assert "MOMENTUM_HARD_ENFORCEMENT" in decision.enforcement_flags


def test_rotation_hard_blocks_path_a_and_micro_allows_sweep():
    decision = decide_epic_hard_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={
            "ownership": "ROTATION",
            "confidence": 70,
            "blocked_paths": [ExecutionPath.PATH_A.value, ExecutionPath.MICRO.value],
        },
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "ROTATION", "confidence": 60},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert decision.active is True
    assert ExecutionPath.PATH_B_HANDOFF.value in decision.hard_allow_paths
    assert ExecutionPath.PATH_A.value in decision.hard_block_paths
    assert ExecutionPath.MICRO.value in decision.hard_block_paths
    assert "ROTATION_HARD_ENFORCEMENT" in decision.enforcement_flags


def test_stand_down_hard_blocks_all_paths():
    decision = decide_epic_hard_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={
            "ownership": "STAND_DOWN",
            "confidence": 95,
            "blocked_paths": [p.value for p in ExecutionPath],
        },
        transition_row=None,
        selector_advice={"recommended_strategy_profile": "STAND_DOWN", "confidence": 95},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert decision.active is True
    assert set(decision.hard_block_paths) == {
        ExecutionPath.PATH_A.value,
        ExecutionPath.MICRO.value,
        ExecutionPath.PATH_B_HANDOFF.value,
    }
    assert "STAND_DOWN_HARD" in decision.enforcement_flags


def test_high_confidence_transition_hard_blocks_current_profile():
    decision = decide_epic_hard_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row=_controller_scalp(),
        transition_row={
            "current_profile": "SCALP",
            "target_profile": "MOMENTUM",
            "transition_confidence": 85,
        },
        selector_advice={"recommended_strategy_profile": "MOMENTUM", "confidence": 85},
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_ok(),
    )
    assert decision.active is True
    assert "HIGH_CONFIDENCE_HARD_TRANSITION" in decision.enforcement_flags
    assert ExecutionPath.MICRO.value in decision.hard_block_paths
    assert ExecutionPath.PATH_A.value in decision.hard_allow_paths
    assert ExecutionPath.PATH_A.value not in decision.hard_block_paths


def test_hard_guards_respect_injected_decisions():
    set_hard_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "hard_block_paths": ["PATH_A"],
                "hard_allow_paths": ["MICRO"],
                "enforcement_confidence": 80,
                "enforcement_reason": "test",
                "enforcement_flags": ["SCALP_HARD_ENFORCEMENT"],
                "active": True,
            }
        ]
    )
    assert is_hard_enforcement_active("CS.D.EURUSD.CFD.IP") is True
    assert hard_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
    assert hard_guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is True


def test_hard_enforcement_overrides_soft_enforcement():
    from runtime.strategy_enforcement import soft_guard_path_a_execution

    set_hard_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "hard_block_paths": [],
                "hard_allow_paths": ["PATH_A"],
                "enforcement_confidence": 85,
                "enforcement_reason": "MOMENTUM hard allow",
                "enforcement_flags": ["MOMENTUM_HARD_ENFORCEMENT"],
                "active": True,
            }
        ]
    )
    set_strategy_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "soft_block_paths": ["PATH_A"],
                "soft_allow_paths": ["MICRO"],
                "enforcement_confidence": 80,
                "enforcement_reason": "soft block path a",
                "enforcement_flags": [],
            }
        ]
    )

    assert is_hard_enforcement_active("CS.D.EURUSD.CFD.IP") is True
    assert hard_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is True
    assert soft_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
    # Engine skips soft guard when hard enforcement is active.
    assert not is_hard_enforcement_active("CS.D.EURUSD.CFD.IP") or hard_guard_path_a_execution(
        "CS.D.EURUSD.CFD.IP"
    )


def test_guards_never_call_live_executor_or_place_market_order():
    set_hard_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "hard_block_paths": ["PATH_A", "MICRO", "PATH_B_HANDOFF"],
                "hard_allow_paths": [],
                "enforcement_confidence": 100,
                "enforcement_reason": "STAND_DOWN",
                "enforcement_flags": ["STAND_DOWN_HARD"],
                "active": True,
            }
        ]
    )
    live_executor_cls = MagicMock()
    rest_client = MagicMock()

    with patch.dict(
        "sys.modules",
        {"execution.live_executor": MagicMock(LiveExecutor=live_executor_cls)},
    ):
        assert hard_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
        assert hard_guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is False
        assert hard_guard_path_b_handoff("CS.D.EURUSD.CFD.IP") is False

    live_executor_cls.assert_not_called()
    rest_client.place_market_order.assert_not_called()


def test_execution_engine_hard_guard_returns_without_live_executor():
    from data.models import Quote
    from execution.execution_engine import ExecutionEngine
    from execution.types import ExecutionMode, ExecutionResult, TradeSignal

    set_hard_enforcement_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "hard_block_paths": ["PATH_A"],
                "hard_allow_paths": ["MICRO"],
                "enforcement_confidence": 80,
                "enforcement_reason": "SCALP hard block",
                "enforcement_flags": ["SCALP_HARD_ENFORCEMENT"],
                "active": True,
            }
        ]
    )

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
    assert result.rejection_reason == "hard_blocked_by_strategy_enforcement"
    live_mock.execute.assert_not_called()


def test_gui_status_includes_hard_enforcement_decisions(tmp_path, monkeypatch):
    scope = "ig:HEF1"
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

    stub_hard = [
        {
            "epic": "CS.D.EURUSD.CFD.IP",
            "hard_block_paths": ["PATH_A"],
            "hard_allow_paths": ["MICRO"],
            "enforcement_confidence": 85,
            "enforcement_reason": "SCALP hard enforcement",
            "enforcement_flags": ["SCALP_HARD_ENFORCEMENT"],
            "active": True,
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
        return_value=[],
    ), patch(
        "api.gui_status.build_hard_enforcement_decisions",
        return_value=stub_hard,
    ):
        status = build_gui_status()

    assert "hard_enforcement_decisions" in status
    row = status["hard_enforcement_decisions"][0]
    assert row["epic"] == "CS.D.EURUSD.CFD.IP"
    assert row["hard_block_paths"] == ["PATH_A"]
    assert row["hard_allow_paths"] == ["MICRO"]
    assert row["enforcement_confidence"] == 85
    assert row["enforcement_flags"] == ["SCALP_HARD_ENFORCEMENT"]


def test_build_hard_enforcement_decisions_shape():
    rows = build_hard_enforcement_decisions(
        trade_pipeline_health=[
            {"epic": "CS.D.EURUSD.CFD.IP", "active_strategy_profile": "SCALP"}
        ],
        pipeline_governance={"per_epic": []},
        api_feed_health=_feed_ok(),
        strategy_controller_decisions=[
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "ownership": "SCALP",
                "confidence": 75,
                "blocked_paths": ["PATH_A"],
            }
        ],
        strategy_transition_advice=[],
        strategy_selector_advice=[],
    )
    assert len(rows) == 1
    item = rows[0]
    assert item["epic"] == "CS.D.EURUSD.CFD.IP"
    assert isinstance(item["hard_block_paths"], list)
    assert isinstance(item["hard_allow_paths"], list)
    assert 0 <= item["enforcement_confidence"] <= 100
    assert isinstance(item["enforcement_reason"], str)
    assert isinstance(item["enforcement_flags"], list)
    assert item["active"] is True


def test_feed_degraded_activates_hard_enforcement():
    decision = decide_epic_hard_enforcement(
        "CS.D.EURUSD.CFD.IP",
        controller_row={
            "ownership": "MOMENTUM",
            "confidence": 80,
            "blocked_paths": [],
        },
        transition_row=None,
        selector_advice=None,
        gov_row={"pipeline_anomalies": [], "feed_anomalies": []},
        api_feed_health=_feed_degraded(),
    )
    assert decision.active is True
    assert "FEED_DEGRADED" in decision.enforcement_flags
