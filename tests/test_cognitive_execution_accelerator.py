"""Cognitive Reasoner HUD + trading ability accelerators — integration suite."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_get_cognitive_reasoning_string_hold_with_spread_telemetry():
    from trading.probability_engine import compile_cognitive_reasoning, get_cognitive_reasoning_string

    explore = {
        "market_rankings": [{"epic": "CS.D.EURUSD.CFD.IP", "score": 0.52}],
        "correlation_exposures": [],
        "entry_frozen": False,
    }
    tel = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "spread_pts": 0.8,
        "median_spread_pts": 0.6,
        "adaptive_ceiling_pts": 1.2,
        "expectation_score": 0.52,
        "high_conviction_widen": False,
    }
    with patch(
        "runtime.portfolio_exploration_engine.get_exploration_state_snapshot",
        return_value=explore,
    ), patch(
        "runtime.portfolio_exploration_engine.adaptive_spread_telemetry",
        return_value=tel,
    ), patch(
        "runtime.portfolio_exploration_engine.vet_order_spread",
        return_value=(True, "", 1.2),
    ), patch(
        "runtime.master_orchestrator.get_platform_scoreboard"
    ) as mock_sb:
        mock_sb.return_value = MagicMock(
            telemetry_tier_unlocked=lambda: "standard",
            total_pp=900,
        )
        bundle = compile_cognitive_reasoning(epic="CS.D.EURUSD.CFD.IP")
        text = get_cognitive_reasoning_string(epic="CS.D.EURUSD.CFD.IP")

    assert "HOLD" in bundle["text"] or "CS.D.EURUSD" in bundle["text"]
    assert bundle["severity"] == "normal"
    assert text == bundle["text"]


def test_cognitive_reasoning_execution_window_on_high_ml():
    from trading.probability_engine import compile_cognitive_reasoning

    explore = {
        "market_rankings": [{"epic": "CS.D.CFPGOLD.CFP.IP", "score": 0.71}],
        "correlation_exposures": [],
        "entry_frozen": False,
    }
    with patch(
        "runtime.portfolio_exploration_engine.get_exploration_state_snapshot",
        return_value=explore,
    ), patch(
        "runtime.portfolio_exploration_engine.adaptive_spread_telemetry",
        return_value={
            "epic": "CS.D.CFPGOLD.CFP.IP",
            "spread_pts": 1.0,
            "adaptive_ceiling_pts": 2.5,
            "expectation_score": 0.71,
            "high_conviction_widen": True,
        },
    ), patch(
        "runtime.portfolio_exploration_engine.vet_order_spread",
        return_value=(True, "", 2.5),
    ), patch("runtime.master_orchestrator.get_platform_scoreboard") as mock_sb:
        mock_sb.return_value = MagicMock(
            telemetry_tier_unlocked=lambda: "emerald_expansion",
            total_pp=1250,
        )
        bundle = compile_cognitive_reasoning(epic="CS.D.CFPGOLD.CFP.IP")

    assert bundle["severity"] == "execution_window"
    assert "EXECUTION WINDOW" in bundle["text"]


def test_adaptive_spread_ceiling_widens_above_ml_threshold():
    from runtime.portfolio_exploration_engine import (
        ADAPTIVE_SPREAD_MEDIAN_MULT,
        HIGH_CONVICTION_ML_THRESHOLD,
        HIGH_CONVICTION_SPREAD_WIDEN,
        resolve_adaptive_spread_ceiling,
    )

    with patch(
        "runtime.portfolio_exploration_engine.historical_median_spread_pts",
        return_value=2.0,
    ):
        base = resolve_adaptive_spread_ceiling("CS.D.EURUSD.CFD.IP", expectation_score=0.50)
        wide = resolve_adaptive_spread_ceiling(
            "CS.D.EURUSD.CFD.IP",
            expectation_score=HIGH_CONVICTION_ML_THRESHOLD + 0.05,
        )

    assert base == pytest.approx(2.0 * ADAPTIVE_SPREAD_MEDIAN_MULT)
    assert wide == pytest.approx(base * HIGH_CONVICTION_SPREAD_WIDEN)


def test_vet_order_spread_rejects_above_ceiling():
    from runtime.portfolio_exploration_engine import vet_order_spread

    with patch(
        "runtime.portfolio_exploration_engine.resolve_adaptive_spread_ceiling",
        return_value=3.0,
    ):
        ok, reason, ceiling = vet_order_spread("CS.D.EURUSD.CFD.IP", 4.5, expectation_score=0.7)

    assert ok is False
    assert "adaptive_ceiling" in reason
    assert ceiling == 3.0


def test_flash_allocation_emerald_limit_chase_regime0():
    from runtime.portfolio_exploration_engine import evaluate_flash_allocation

    with patch("runtime.master_orchestrator.get_platform_scoreboard") as mock_sb, patch(
        "runtime.master_orchestrator.TELEMETRY_TIER_EMERALD",
        "emerald_expansion",
    ), patch("runtime.master_orchestrator.PP_EXPANSION_THRESHOLD", 1200):
        mock_sb.return_value = MagicMock(
            telemetry_tier_unlocked=lambda: "emerald_expansion",
            total_pp=1300,
        )
        assert evaluate_flash_allocation(
            execution_path="limit_chase_hf",
            regime_state=0,
            target_hold_sec=8.0,
        )
        assert not evaluate_flash_allocation(
            execution_path="limit_chase_hf",
            regime_state=0,
            target_hold_sec=12.0,
        )
        assert not evaluate_flash_allocation(
            execution_path="momentum_breakout",
            regime_state=0,
            target_hold_sec=5.0,
        )


def test_risk_manager_flash_raises_kelly_cap():
    from execution.risk_manager import RiskManager

    cfg = MagicMock()
    cfg.trade_size = 1.0
    cfg.stop_distance_points = 10.0
    cfg.reward_multiple = 2.0
    cfg.max_spread_points = 50.0
    cfg.adaptive_max_trade_size = 10.0
    cfg.adaptive_min_trade_size = 0.1
    cfg.adaptive_max_risk_points = 100.0
    cfg.adaptive_min_risk_points = 1.0
    cfg.min_account_available = 0
    cfg.min_account_balance = 0
    cfg.max_daily_loss_gbp = 0
    cfg.max_daily_trades = 0
    cfg.max_open_risk_points = 0

    rm = RiskManager(cfg, store=None)
    params = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "size": 1.0,
        "risk": 10.0,
        "limit": 20.0,
        "spread": 1.0,
        "execution_path": "limit_chase_hf",
        "regime_state": 0,
        "target_hold_sec": 6.0,
        "win_probability": 0.7,
        "atr": 5.0,
        "contract_multiplier": 1.0,
    }

    with patch(
        "runtime.portfolio_exploration_engine.evaluate_flash_allocation",
        return_value=True,
    ), patch(
        "runtime.master_orchestrator.get_strategy_route",
        return_value={"execution_path": "limit_chase_hf", "kelly_fraction": 0.0},
    ), patch.object(rm, "_cfg", cfg), patch(
        "execution.risk_manager.resolve_atr_for_epic",
        return_value=5.0,
    ), patch(
        "execution.risk_manager.resolve_contract_multiplier",
        return_value=1.0,
    ), patch(
        "runtime.portfolio_exploration_engine._estimate_margin_used",
        return_value=0.0,
    ), patch(
        "runtime.portfolio_exploration_engine._load_open_book",
        return_value=[],
    ), patch(
        "runtime.portfolio_exploration_engine.vet_order_spread",
        return_value=(True, "", 5.0),
    ), patch(
        "runtime.portfolio_exploration_engine.assess_portfolio_exploration",
        return_value=MagicMock(approved=True, size=1.0, reason=""),
    ), patch("execution.economic_check.check_risk_cap", return_value=(True, 10.0, 500.0)):
        result = rm.assess(direction="BUY", execution_params=params)

    assert result.approved is True
    # Flash raises base cap to 0.22; continuous ML scaler applies conviction curve.
    assert result.kelly_fraction >= 0.10
    assert result.kelly_fraction <= 0.22


def test_orchestrator_state_includes_cognitive_reason():
    from runtime import master_orchestrator as mo

    mo.reset_master_orchestrator_for_tests()
    with patch(
        "trading.probability_engine.compile_cognitive_reasoning",
        return_value={
            "text": "HOLD — test counsel",
            "severity": "normal",
            "adaptive_spread_ceiling": 2.0,
            "spread_pts": 1.0,
            "epic": "CS.D.EURUSD.CFD.IP",
        },
    ), patch.object(mo, "_primed", True), patch.object(
        mo, "is_warming_up",
        return_value=False,
    ), patch.object(
        mo, "all_warmup_phases_healthy",
        return_value=True,
    ), patch.object(
        mo, "all_warmup_phases_acceptable",
        return_value=True,
    ), patch(
        "runtime.portfolio_exploration_engine.get_exploration_state_snapshot",
        return_value={"position_tree": [], "adaptive_spread_telemetry": []},
    ):
        body = mo._compose_orchestrator_snapshot_body()

    assert body["cognitive_reason"] == "HOLD — test counsel"
    assert body["cognitive_reason_severity"] == "normal"
    assert body["cognitive_reason_meta"]["epic"] == "CS.D.EURUSD.CFD.IP"
    mo.reset_master_orchestrator_for_tests()


def test_cockpit_cognitive_reasoner_panel_layout_simulation():
    """Layout contract — panel ids present for rAF renderer."""
    from pathlib import Path

    html = Path(__file__).resolve().parents[1].joinpath("cockpit-web", "index.html").read_text(
        encoding="utf-8"
    )
    assert "cognitive-reasoner-panel" in html
    assert "cognitive-reasoner-text" in html
    assert "Avionics Strategic Counsel" in html

    app_js = Path(__file__).resolve().parents[1].joinpath("cockpit-web", "app.js").read_text(
        encoding="utf-8"
    )
    assert "scheduleCognitiveReasonerRender" in app_js
    assert "counsel-execution" in app_js
    assert "requestAnimationFrame" in app_js

    css = Path(__file__).resolve().parents[1].joinpath("cockpit-web", "styles.css").read_text(
        encoding="utf-8"
    )
    assert ".counsel-near-miss" in css
    assert ".counsel-execution" in css
