"""Direction quality + profit-policy fail-closed for threshold / missing ML."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from diagnostics.direction_quality import score_direction_quality
from ml.profit_policy import apply_profit_policy


def test_direction_quality_sell_heavy_adverse_not_wire_swap(tmp_path: Path) -> None:
    journal = tmp_path / "daily_journal.csv"
    journal.write_text(
        "Timestamp,DealID,Direction,EntryPrice,ExitPrice,RealizedPnL_GBP\n"
        "2026-07-24T01:00:00Z,D1,SELL,100,105,-5\n"
        "2026-07-24T01:01:00Z,D2,SELL,100,103,-3\n"
        "2026-07-24T01:02:00Z,D3,BUY,100,97,-3\n",
        encoding="utf-8",
    )
    losers = [
        {"deal_id": "D1", "direction": "SELL", "ml_score_at_entry": 0.43, "pnl_gbp": -5},
        {"deal_id": "D2", "direction": "SELL", "ml_score_at_entry": 0.68, "pnl_gbp": -3},
        {"deal_id": "D3", "direction": "BUY", "ml_score_at_entry": 0.77, "pnl_gbp": -3},
    ]
    out = score_direction_quality(losers, journal_path=journal)
    assert out["adverse_to_side"] == 3
    assert out["wire_inversion_suspected"] is False
    assert out["side_counts"]["SELL"] == 2
    assert out["weak_ml_among_adverse"] >= 2
    assert "NOT a BUY↔SELL" in out["verdict"] or "wire" in out["verdict"].lower() or "ADVERSE" in out["verdict"]


def test_profit_policy_vetoes_threshold_constant() -> None:
    cfg = MagicMock()
    cfg.get = lambda key, default=None: {
        "enabled": True,
        "marginal_ml_veto": True,
        "require_ml_probability": True,
        "min_ml_probability": 0.52,
    } if key == "profit_philosophy" else default
    v = apply_profit_policy(cfg, 70.0, ml_prob=0.68)
    assert v.veto is True
    assert "threshold" in v.reason.lower()


def test_profit_policy_vetoes_absent_ml_when_required() -> None:
    cfg = MagicMock()
    cfg.get = lambda key, default=None: {
        "enabled": True,
        "marginal_ml_veto": True,
        "require_ml_probability": True,
        "min_ml_probability": 0.52,
    } if key == "profit_philosophy" else default
    v = apply_profit_policy(cfg, 70.0, ml_prob=None)
    assert v.veto is True
    assert "absent" in v.reason.lower() or "require" in v.reason.lower()


def test_profit_policy_vetoes_below_floor() -> None:
    cfg = MagicMock()
    cfg.get = lambda key, default=None: {
        "enabled": True,
        "marginal_ml_veto": True,
        "require_ml_probability": True,
        "min_ml_probability": 0.52,
    } if key == "profit_philosophy" else default
    v = apply_profit_policy(cfg, 70.0, ml_prob=0.437)
    assert v.veto is True
    assert "0.437" in v.reason


def test_sniper_gate_rejects_threshold_constant() -> None:
    from execution.entry_gate_hardening import evaluate_sniper_ml_gate
    from alpha.micro_sniper_ml import SniperProbabilityResult, SNIPER_THRESHOLD
    from unittest.mock import patch

    fake = SniperProbabilityResult(
        p_success=float(SNIPER_THRESHOLD),
        approved=True,
        threshold=float(SNIPER_THRESHOLD),
        logit=0.0,
        features={},
        reason="sniper_ml_ok",
    )
    with patch(
        "alpha.micro_sniper_ml.evaluate_live_sniper_probability",
        return_value=fake,
    ):
        ok, reason, p = evaluate_sniper_ml_gate("IX.D.DOW.IFM.IP", "SELL")
    assert ok is False
    assert "threshold_constant" in reason
    assert abs(p - SNIPER_THRESHOLD) < 1e-9


def test_resolve_ml_refuses_threshold_epic_snapshot() -> None:
    from diagnostics.ml_trade_outcomes import resolve_ml_score_and_source
    from unittest.mock import patch

    with patch(
        "alpha.micro_sniper_ml.latest_sniper_ml_snapshot",
        return_value={"p_success": 0.68},
    ):
        score, source = resolve_ml_score_and_source(epic="IX.D.DOW.IFM.IP")
    assert score is None
    assert source == "absent"