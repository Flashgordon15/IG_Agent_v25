"""Automated Expectancy Optimization Engine — Kelly scaler, slippage TP, scoreboard veto."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from execution.risk_manager import (
    compute_continuous_kelly_fraction,
    compute_kelly_position_size,
)
from runtime import parameter_tuner as pt
from runtime import portfolio_exploration_engine as pee
from runtime.master_orchestrator import (
    PP_DEFENSE_THRESHOLD,
    PP_EXPANSION_THRESHOLD,
    PlatformScoreboard,
    _compose_orchestrator_snapshot_body,
)
from trading.probability_engine import (
    VETO_FLOOR_DEFENSIVE_PP,
    VETO_FLOOR_EXPANSION_PP,
    WIN_VETO_FLOOR_STRICT,
    reset_cognitive_self_correction_for_tests,
    resolve_dynamic_veto_floor,
    resolve_scoreboard_veto_floor,
)


@pytest.fixture(autouse=True)
def _reset_probability_state():
    reset_cognitive_self_correction_for_tests()
    yield
    reset_cognitive_self_correction_for_tests()


def test_continuous_kelly_at_veto_floor_is_zero():
    eff = compute_continuous_kelly_fraction(
        base_kelly_cap=0.15,
        ml_expectation_score=0.55,
        veto_floor=0.55,
    )
    assert eff == pytest.approx(0.0)


def test_continuous_kelly_scales_linearly_mid_band():
    eff = compute_continuous_kelly_fraction(
        base_kelly_cap=0.15,
        ml_expectation_score=0.625,
        veto_floor=0.55,
    )
    expected = 0.15 * ((0.625 - 0.55) / (1.0 - 0.55))
    assert eff == pytest.approx(expected)


def test_continuous_kelly_unlocks_near_high_conviction():
    eff = compute_continuous_kelly_fraction(
        base_kelly_cap=0.25,
        ml_expectation_score=0.70,
        veto_floor=0.55,
    )
    assert eff >= 0.25 * 0.65
    assert eff <= 0.25

    full = compute_continuous_kelly_fraction(
        base_kelly_cap=0.25,
        ml_expectation_score=1.0,
        veto_floor=0.55,
    )
    assert full == pytest.approx(0.25)


def test_kelly_position_size_without_ml_uses_static_fraction():
    size = compute_kelly_position_size(
        equity=10_000.0,
        kelly_fraction=0.15,
        atr=50.0,
        contract_multiplier=2.0,
    )
    assert size == pytest.approx((10_000 * 0.15) / (50 * 2))


def test_kelly_position_size_with_ml_applies_continuous_scaler():
    size = compute_kelly_position_size(
        equity=10_000.0,
        kelly_fraction=0.15,
        atr=50.0,
        contract_multiplier=2.0,
        ml_expectation_score=0.70,
        veto_floor=0.55,
    )
    eff = compute_continuous_kelly_fraction(
        base_kelly_cap=0.15,
        ml_expectation_score=0.70,
        veto_floor=0.55,
    )
    assert size == pytest.approx((10_000 * eff) / (50 * 2))


def test_scoreboard_expansion_lowers_veto_floor():
    sb = PlatformScoreboard(baseline=PP_EXPANSION_THRESHOLD)
    with patch("runtime.master_orchestrator.get_platform_scoreboard", return_value=sb):
        assert resolve_scoreboard_veto_floor() == VETO_FLOOR_EXPANSION_PP
        assert resolve_dynamic_veto_floor() == VETO_FLOOR_EXPANSION_PP


def test_scoreboard_defensive_raises_veto_floor():
    sb = PlatformScoreboard(baseline=PP_DEFENSE_THRESHOLD - 50)
    with patch("runtime.master_orchestrator.get_platform_scoreboard", return_value=sb):
        assert resolve_scoreboard_veto_floor() == VETO_FLOOR_DEFENSIVE_PP
        assert resolve_dynamic_veto_floor() == VETO_FLOOR_DEFENSIVE_PP


def test_scoreboard_neutral_uses_strict_floor():
    sb = PlatformScoreboard(baseline=1000)
    with patch("runtime.master_orchestrator.get_platform_scoreboard", return_value=sb):
        assert resolve_scoreboard_veto_floor() == WIN_VETO_FLOOR_STRICT


def test_slippage_adaptive_offsets_expand_above_threshold():
    rolling = {"CS.D.CFPGOLD.CFP.IP": 0.62}
    with patch.object(pt, "_rolling_slippage_by_epic", return_value=rolling):
        offsets = pt.compute_slippage_adaptive_offsets()
    row = offsets["CS.D.CFPGOLD.CFP.IP"]
    assert row["active"] is True
    assert row["limit_factor_mult"] == pytest.approx(pt.SLIPPAGE_TP_EXPANSION_MULT)
    assert row["profit_target_multiplier"] == pytest.approx(pt.SLIPPAGE_TP_EXPANSION_MULT)


def test_apply_slippage_adaptive_take_profit_widens_regime_row():
    matrix = pt.get_regime_matrix()
    base_limit = matrix["0"]["limit_factor"]
    offsets = {
        "CS.D.CFPGOLD.CFP.IP": {
            "active": True,
            "limit_factor_mult": pt.SLIPPAGE_TP_EXPANSION_MULT,
            "profit_target_multiplier": pt.SLIPPAGE_TP_EXPANSION_MULT,
        }
    }
    with patch.object(pt, "_infer_regime_for_epic", return_value=0):
        new_matrix, reasons = pt.apply_slippage_adaptive_take_profit(matrix, offsets)
    assert new_matrix["0"]["limit_factor"] > base_limit
    assert any("slippage_tp_expansion" in r for r in reasons)


def test_expectancy_metrics_snapshot_serializes_rows():
    pee.reset_portfolio_exploration_for_tests()
    pee.inject_exploration_rankings_for_tests(
        [
            {
                "epic": "CS.D.CFPGOLD.CFP.IP",
                "score": 0.72,
                "rank": 1,
            }
        ]
    )
    with patch(
        "runtime.master_orchestrator.get_strategy_route",
        return_value={"kelly_fraction": 0.15, "execution_path": "limit_chase_hf"},
    ):
        with patch.object(
            pt,
            "get_slippage_adaptive_offsets",
            return_value={
                "CS.D.CFPGOLD.CFP.IP": {
                    "avg_slippage_pips": 0.6,
                    "limit_factor_mult": 1.35,
                    "profit_target_multiplier": 1.35,
                    "active": True,
                }
            },
        ):
            rows = pee.get_expectancy_metrics_snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row["epic"] == "CS.D.CFPGOLD.CFP.IP"
    assert row["ml_expectation_score"] == pytest.approx(0.72)
    assert row["continuous_kelly_fraction"] > 0
    assert row["slippage_adaptive_active"] is True


def test_orchestrator_snapshot_includes_expectancy_metrics():
    with patch(
        "runtime.portfolio_exploration_engine.get_expectancy_metrics_snapshot",
        return_value=[{"epic": "IX.D.DOW.IFM.IP", "continuous_kelly_fraction": 0.08}],
    ):
        with patch("trading.probability_engine.compile_cognitive_reasoning", return_value={"text": "ok"}):
            body = _compose_orchestrator_snapshot_body()
    assert "expectancy_metrics" in body
    assert body["expectancy_metrics"][0]["epic"] == "IX.D.DOW.IFM.IP"
