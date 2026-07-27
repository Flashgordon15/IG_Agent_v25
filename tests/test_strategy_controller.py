"""Strategy controller permissions and guard tests — no execution side effects."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import (
    ExecutionPath,
    StrategyOwnership,
    build_strategy_controller_decisions,
    decide_epic,
    guard_micro_dispatch,
    guard_path_a_execution,
    guard_path_b_handoff,
    is_path_allowed,
    reset_strategy_controller_for_tests,
    set_strategy_controller_decisions_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_strategy_controller_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _epic_row(**overrides) -> dict:
    base = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "active_strategy_profile": "UNKNOWN",
        "strategy_source": "NONE",
    }
    base.update(overrides)
    return base


def test_scalp_blocks_path_a_allows_micro():
    decision = decide_epic(_epic_row(active_strategy_profile="SCALP", strategy_source="MICRO"))
    assert ExecutionPath.PATH_A.value in [p.value for p in decision.blocked_paths]
    assert ExecutionPath.MICRO.value in [p.value for p in decision.allowed_paths]
    assert ExecutionPath.PATH_B_HANDOFF.value in [p.value for p in decision.allowed_paths]
    assert "SCALP_OWNS_EPIC" in decision.enforcement_flags


def test_sb_macro_scalp_carve_allows_path_a(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("IG_ENGINE_ORIGIN", "MACRO_SENTINEL")
    monkeypatch.setenv("IG_ACCOUNT_ID", "Z6BAH3")
    cfg = {
        "dual_regime": {
            "enabled": True,
            "sb_disable_instant_micro": True,
            "sb_disable_core_b_micro": True,
            "sb_macro_ltr_entries_only": True,
        }
    }
    with patch("system.config_loader.get_config", return_value=cfg):
        decision = decide_epic(
            _epic_row(active_strategy_profile="SCALP", strategy_source="MICRO")
        )
    assert ExecutionPath.PATH_A.value in [p.value for p in decision.allowed_paths]
    assert ExecutionPath.PATH_A.value not in [p.value for p in decision.blocked_paths]
    assert ExecutionPath.MICRO.value in [p.value for p in decision.blocked_paths]
    assert "SB_MACRO_PATH_A_CARVE" in decision.enforcement_flags


def test_momentum_blocks_micro_allows_path_a():
    decision = decide_epic(_epic_row(active_strategy_profile="MOMENTUM", strategy_source="PATH_A"))
    assert ExecutionPath.PATH_A.value in [p.value for p in decision.allowed_paths]
    assert ExecutionPath.MICRO.value in [p.value for p in decision.blocked_paths]
    assert ExecutionPath.PATH_B_HANDOFF.value in [p.value for p in decision.blocked_paths]
    assert "MOMENTUM_OWNS_EPIC" in decision.enforcement_flags


def test_swing_blocks_micro_allows_path_a():
    decision = decide_epic(_epic_row(active_strategy_profile="SWING", strategy_source="PATH_A"))
    assert ExecutionPath.PATH_A.value in [p.value for p in decision.allowed_paths]
    assert ExecutionPath.MICRO.value in [p.value for p in decision.blocked_paths]
    assert "SWING_OWNS_EPIC" in decision.enforcement_flags


def test_rotation_blocks_execution_allows_sweep_handoff():
    decision = decide_epic(
        _epic_row(active_strategy_profile="ROTATION", strategy_source="PATH_B_HANDOFF"),
    )
    assert ExecutionPath.PATH_A.value in [p.value for p in decision.blocked_paths]
    assert ExecutionPath.MICRO.value in [p.value for p in decision.blocked_paths]
    assert ExecutionPath.PATH_B_HANDOFF.value in [p.value for p in decision.allowed_paths]
    assert "ROTATION_ACTIVE" in decision.enforcement_flags


def test_rotation_allows_micro_when_selector_scalp_high_confidence():
    decision = decide_epic(
        _epic_row(active_strategy_profile="ROTATION", strategy_source="PATH_B_HANDOFF"),
        advice_row={
            "epic": "CS.D.EURUSD.CFD.IP",
            "recommended_strategy_profile": "SCALP",
            "confidence": 85,
        },
    )
    assert ExecutionPath.MICRO.value in [p.value for p in decision.allowed_paths]
    assert ExecutionPath.PATH_A.value in [p.value for p in decision.blocked_paths]


def test_stand_down_blocks_all_paths():
    decision = decide_epic(
        _epic_row(active_strategy_profile="STAND_DOWN"),
        advice_row={
            "epic": "CS.D.EURUSD.CFD.IP",
            "recommended_strategy_profile": "STAND_DOWN",
            "confidence": 90,
        },
    )
    assert decision.allowed_paths == []
    assert len(decision.blocked_paths) == 3
    assert "STAND_DOWN" in decision.enforcement_flags


def test_guard_path_a_respects_decisions():
    set_strategy_controller_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "ownership": StrategyOwnership.SCALP.value,
                "allowed_paths": ["MICRO", "PATH_B_HANDOFF"],
                "blocked_paths": ["PATH_A"],
                "reason": "SCALP owns epic",
                "confidence": 80,
                "enforcement_flags": ["SCALP_OWNS_EPIC"],
            }
        ]
    )
    assert guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
    assert is_path_allowed("CS.D.EURUSD.CFD.IP", ExecutionPath.MICRO) is True


def test_guard_micro_respects_momentum_ownership():
    set_strategy_controller_decisions_for_tests(
        [
            {
                "epic": "IX.D.DOW.IFM.IP",
                "ownership": StrategyOwnership.MOMENTUM.value,
                "allowed_paths": ["PATH_A"],
                "blocked_paths": ["MICRO", "PATH_B_HANDOFF"],
                "reason": "MOMENTUM owns epic",
                "confidence": 75,
                "enforcement_flags": ["MOMENTUM_OWNS_EPIC"],
            }
        ]
    )
    assert guard_micro_dispatch("IX.D.DOW.IFM.IP") is False
    assert guard_path_a_execution("IX.D.DOW.IFM.IP") is True


def test_guard_path_b_handoff_respects_rotation():
    set_strategy_controller_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "ownership": StrategyOwnership.ROTATION.value,
                "allowed_paths": ["PATH_B_HANDOFF"],
                "blocked_paths": ["PATH_A", "MICRO"],
                "reason": "ROTATION active",
                "confidence": 70,
                "enforcement_flags": ["ROTATION_ACTIVE"],
            }
        ]
    )
    assert guard_path_b_handoff("CS.D.EURUSD.CFD.IP") is True
    assert guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is False


def test_guards_do_not_call_live_executor_or_place_market_order():
    set_strategy_controller_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "ownership": StrategyOwnership.STAND_DOWN.value,
                "allowed_paths": [],
                "blocked_paths": ["PATH_A", "MICRO", "PATH_B_HANDOFF"],
                "reason": "STAND_DOWN",
                "confidence": 100,
                "enforcement_flags": ["STAND_DOWN"],
            }
        ]
    )
    rest_client = MagicMock()
    live_executor_cls = MagicMock()

    with patch.dict(
        "sys.modules",
        {
            "execution.live_executor": MagicMock(LiveExecutor=live_executor_cls),
        },
    ):
        assert guard_path_a_execution("CS.D.EURUSD.CFD.IP") is False
        assert guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is False
        assert guard_path_b_handoff("CS.D.EURUSD.CFD.IP") is False

    live_executor_cls.assert_not_called()
    rest_client.place_market_order.assert_not_called()


def test_execution_engine_guard_returns_without_live_executor():
    from datetime import datetime

    from datetime import datetime, timezone

    from data.models import Quote
    from execution.execution_engine import ExecutionEngine
    from execution.types import ExecutionMode, ExecutionResult, TradeSignal

    set_strategy_controller_decisions_for_tests(
        [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "ownership": StrategyOwnership.SCALP.value,
                "allowed_paths": ["MICRO"],
                "blocked_paths": ["PATH_A"],
                "reason": "SCALP owns epic",
                "confidence": 80,
                "enforcement_flags": ["SCALP_OWNS_EPIC"],
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
        quote=Quote(datetime(2026, 6, 28, 12, 0), 1.1, 1.1002),
    )

    with patch("execution.execution_engine.get_rate_limit_manager"), patch(
        "execution.atomic_gateway.assert_execution_allowed",
        return_value=None,
    ), patch.object(engine, "_live", MagicMock()) as live_mock:
        result = engine.execute_trade(signal, prevalidated=True)

    assert isinstance(result, ExecutionResult)
    assert result.success is False
    assert result.rejection_reason == "blocked_by_strategy_controller"
    live_mock.execute.assert_not_called()


def test_gui_status_includes_strategy_controller_decisions(tmp_path, monkeypatch):
    scope = "ig:CTRL1"
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

    stub_row = _epic_row(active_strategy_profile="MOMENTUM", strategy_source="PATH_A")
    stub_gov = {
        "pipeline_governance": {"per_epic": []},
        "session_governance": {"overall_session_health_score": 90, "session_anomalies": []},
        "gui_alerts": [],
    }
    stub_advice = [
        {
            "epic": "CS.D.EURUSD.CFD.IP",
            "recommended_strategy_profile": "MOMENTUM",
            "confidence": 80,
        }
    ]
    stub_decisions = [
        {
            "epic": "CS.D.EURUSD.CFD.IP",
            "ownership": "MOMENTUM",
            "allowed_paths": ["PATH_A"],
            "blocked_paths": ["MICRO", "PATH_B_HANDOFF"],
            "reason": "MOMENTUM owns epic",
            "enforcement_flags": ["MOMENTUM_OWNS_EPIC"],
        }
    ]

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[stub_row]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value=stub_gov,
    ), patch(
        "api.gui_status.build_strategy_selector_advice",
        return_value=stub_advice,
    ), patch(
        "api.gui_status.build_strategy_controller_decisions",
        return_value=stub_decisions,
    ):
        status = build_gui_status()

    assert "strategy_controller_decisions" in status
    assert status["strategy_controller_decisions"][0]["allowed_paths"] == ["PATH_A"]
    assert "MICRO" in status["strategy_controller_decisions"][0]["blocked_paths"]


def test_build_strategy_controller_decisions_shape():
    rows = build_strategy_controller_decisions(
        trade_pipeline_health=[_epic_row(active_strategy_profile="SCALP", strategy_source="MICRO")],
        strategy_selector_advice=[],
    )
    assert len(rows) == 1
    item = rows[0]
    assert item["epic"] == "CS.D.EURUSD.CFD.IP"
    assert isinstance(item["allowed_paths"], list)
    assert isinstance(item["blocked_paths"], list)
    assert isinstance(item["reason"], str)
    assert isinstance(item["enforcement_flags"], list)
