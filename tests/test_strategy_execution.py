"""Multi-strategy execution matrix — limit chase, momentum IOC, gates, Kelly sizing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from execution.risk_manager import (
    RiskManager,
    compute_kelly_position_size,
    resolve_atr_for_epic,
)
from runtime import dual_core_execution as dce
from runtime import portfolio_exploration_engine as pee
from system.config import Config


@pytest.fixture(autouse=True)
def _isolate():
    from runtime import master_orchestrator as mo
    from system import chaos_guardian as cg

    dce.reset_strategy_execution_for_tests()
    pee.reset_portfolio_exploration_for_tests()
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    yield
    dce.reset_strategy_execution_for_tests()
    pee.reset_portfolio_exploration_for_tests()
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()


def test_limit_chase_places_at_best_bid_for_long():
    plan = dce.build_limit_chase_plan(
        epic="CS.D.EURUSD.CFD.IP",
        direction="BUY",
        bid=1.0850,
        offer=1.0852,
        size=0.5,
    )
    assert plan.approved is True
    assert plan.route == dce.ROUTE_LIMIT_CHASE_HF
    assert plan.order_type == "LIMIT"
    assert plan.limit_price == pytest.approx(1.0850)
    assert plan.kelly_cap == dce.KELLY_CAP_LIMIT_CHASE


def test_limit_chase_places_at_best_ask_for_short():
    plan = dce.build_limit_chase_plan(
        epic="CS.D.EURUSD.CFD.IP",
        direction="SELL",
        bid=1.0850,
        offer=1.0852,
        size=0.5,
    )
    assert plan.limit_price == pytest.approx(1.0852)


def test_limit_chase_cancels_after_three_ticks():
    epic = "IX.D.DOW.IFM.IP"
    plan0 = dce.build_limit_chase_plan(
        epic=epic, direction="BUY", bid=42000.0, offer=42001.0, size=0.2
    )
    assert plan0.approved is True
    for i in range(1, 4):
        plan = dce.build_limit_chase_plan(
            epic=epic,
            direction="BUY",
            bid=42000.0 + i,
            offer=42001.0 + i,
            size=0.2,
        )
        assert plan.approved is True
    blocked = dce.build_limit_chase_plan(
        epic=epic,
        direction="BUY",
        bid=42004.0,
        offer=42005.0,
        size=0.2,
    )
    assert blocked.approved is False
    assert blocked.reason == "limit_chase_max_ticks_exceeded"


def test_momentum_breakout_ioc_plan():
    plan = dce.build_momentum_breakout_plan(
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        size=0.3,
        z_score=2.5,
    )
    assert plan.route == dce.ROUTE_MOMENTUM_BREAKOUT
    assert plan.order_type == "MARKET_IOC"
    assert plan.kelly_cap == dce.KELLY_CAP_MOMENTUM


def test_expectation_score_gate_blocks_low_score():
    score = pee.compute_expectation_score(confidence=0.5, profit_factor=0.8, multiplier_overlay=1.0)
    assert score == pytest.approx(0.4)
    assert score <= pee.EXPECTATION_SCORE_MIN
    pee.inject_exploration_rankings_for_tests(
        [{"epic": "IX.D.DOW.IFM.IP", "score": 0.4, "confidence": 0.5, "profit_factor": 0.8, "size_factor": 1.0}],
        max_concurrent=5,
    )
    ok, reason = pee.passes_strategy_entry_gates("IX.D.DOW.IFM.IP", "BUY", z_score=-2.0)
    assert ok is False
    assert "expectation_score" in reason


def test_correlation_block_above_threshold():
    epic_a = "IX.D.DOW.IFM.IP"
    epic_b = "IX.D.NIKKEI.IFM.IP"
    n = 60
    rets = np.linspace(-0.01, 0.01, n)
    with patch.object(pee, "_log_returns", side_effect=lambda e, n=288: rets if e in (epic_a, epic_b) else None):
        blocked, reason, _ = pee.correlation_blocks_entry(
            epic_a,
            "BUY",
            [{"epic": epic_b, "direction": "BUY"}],
        )
    assert blocked is True
    assert "correlation" in reason


def test_margin_freeze_at_9500():
    frozen, reason = pee.is_margin_entry_frozen(9600.0)
    assert frozen is True
    assert "margin_freeze" in reason or "margin_util" in reason


def test_hard_margin_ceiling_blocks_proposed_entry():
    pee.inject_exploration_rankings_for_tests(
        [{"epic": "CS.D.CFPGOLD.CFP.IP", "score": 0.8, "confidence": 0.7, "profit_factor": 1.2, "size_factor": 1.0}],
        max_concurrent=20,
        margin_used_gbp=9800.0,
    )
    with patch.object(pee, "_load_open_book", return_value=[{"epic": "IX.D.DOW.IFM.IP", "direction": "BUY", "size": 1.0}]):
        with patch.object(pee, "_estimate_margin_used", return_value=9800.0):
            with patch.object(
                pee,
                "regime_direction_aligned",
                return_value=(True, ""),
            ):
                with patch.object(pee, "correlation_blocks_entry", return_value=(False, "", [])):
                    ok, reason = pee.passes_strategy_entry_gates("CS.D.CFPGOLD.CFP.IP", "BUY", z_score=2.0)
    assert ok is False
    assert "margin_ceiling" in reason or "margin_freeze" in reason


def test_kelly_position_size_formula():
    size = compute_kelly_position_size(
        equity=10_000.0,
        kelly_fraction=0.15,
        atr=50.0,
        contract_multiplier=2.0,
    )
    assert size == pytest.approx((10_000 * 0.15) / (50 * 2))


def test_risk_manager_applies_kelly_sizing():
    cfg = Config(
        _data={
            "trade_size": 1.0,
            "stop_distance_points": 40,
            "reward_multiple": 2.0,
            "max_spread_points": 100,
            "max_spread": 100,
            "adaptive_min_trade_size": 0.01,
            "adaptive_max_trade_size": 50,
            "adaptive_min_risk_points": 10,
            "adaptive_max_risk_points": 200,
            "max_daily_loss": 500,
            "risk_points": 40,
            "risk_cap_gbp": 5000,
            "ig_point_value_gbp": 1.0,
        }
    )
    rm = RiskManager(cfg)
    with patch("execution.risk_manager.resolve_atr_for_epic", return_value=25.0):
        with patch("execution.risk_manager.resolve_contract_multiplier", return_value=2.0):
            with patch("runtime.portfolio_exploration_engine.assess_portfolio_exploration") as pe:
                pe.return_value = MagicMock(approved=True, size=(10_000 * 0.15) / (25 * 2), reason="")
                with patch("system.volatility_risk_engine.apply_volatility_risk") as vr:
                    kelly_size = (10_000 * 0.15) / (25 * 2)
                    vr.return_value = MagicMock(
                        approved=True,
                        size=kelly_size,
                        stop_distance=40,
                        limit_distance=80,
                        reason="ok",
                    )
                    result = rm.assess(
                        direction="BUY",
                        execution_params={
                            "epic": "IX.D.DOW.IFM.IP",
                            "execution_path": "limit_chase_hf",
                            "kelly_fraction": 0.15,
                            "size": 1.0,
                            "risk": 40,
                            "spread": 1.0,
                        },
                        account_balance=10_000.0,
                    )
    assert result.approved is True
    assert result.kelly_fraction == pytest.approx(0.15)
    assert result.size == pytest.approx((10_000 * 0.15) / (25 * 2))


def test_execution_telemetry_in_orchestrator_snapshot():
    dce.build_momentum_breakout_plan(epic="IX.D.DOW.IFM.IP", direction="BUY", size=0.1)
    telem = dce.get_strategy_execution_telemetry()
    assert telem["ok"] is True
    assert len(telem["execution_log"]) >= 1
    assert "IX.D.DOW.IFM.IP" in telem["active_selections"]
