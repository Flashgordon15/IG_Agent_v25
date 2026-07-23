"""Adaptive ATR volatility bracket — size scale + TP/SL blend."""

from __future__ import annotations

from execution.adaptive_atr_bracket import (
    resolve_adaptive_entry_bracket,
    size_scale_for_vol_ratio,
)


CFG = {
    "micro_risk": {
        "risk_per_trade_gbp": 4.0,
        "target_r_multiple": 2.0,
        "min_profit_target_pts": 1.0,
        "max_loss_cap_pts": 4.0,
        "virtual_stop_ceiling_pts": 4.0,
    },
    "volatility_bracket": {
        "enabled": True,
        "initial_stop_atr_mult": 2.5,
        "base_trail_atr_mult": 2.0,
        "size_scale_vol_ratio_ceil": 1.35,
        "size_scale_floor": 0.35,
        "elevated_vol_reward_risk": 2.75,
        "slip_rr_haircut": 0.25,
    },
}


def test_size_scale_shrinks_in_extreme_vol():
    assert size_scale_for_vol_ratio(1.0, cfg=CFG) == 1.0
    assert size_scale_for_vol_ratio(1.35, cfg=CFG) == 1.0
    scaled = size_scale_for_vol_ratio(2.7, cfg=CFG)
    assert scaled < 1.0
    assert scaled >= 0.35


def test_adaptive_bracket_scales_size(monkeypatch):
    monkeypatch.setattr(
        "execution.adaptive_atr_bracket._resolve_atrs",
        lambda epic: (20.0, 10.0),  # vol_ratio = 2.0
    )
    result = resolve_adaptive_entry_bracket(
        "IX.D.DOW.IFM.IP",
        "BUY",
        0.5,
        CFG,
        entry=45000.0,
    )
    assert result.size < 0.5
    assert result.vol_ratio == 2.0
    assert result.sl_pts > 0
    assert result.tp_pts > 0
    # Elevated vol → slip-aware asymmetric R:R (≤2.75 raw, −0.25 haircut → ≥2.5× SL)
    assert result.mode == "asymmetric_rr_elevated_vol"
    assert result.tp_pts >= result.sl_pts * 2.4 - 1e-6
    assert result.tp_pts <= result.sl_pts * 2.8 + 1e-6


def test_adaptive_off_keeps_size(monkeypatch):
    cfg = dict(CFG)
    cfg["volatility_bracket"] = {"enabled": False}
    monkeypatch.setattr(
        "execution.adaptive_atr_bracket._resolve_atrs",
        lambda epic: (20.0, 10.0),
    )
    result = resolve_adaptive_entry_bracket(
        "IX.D.DOW.IFM.IP", "BUY", 0.5, cfg, entry=45000.0
    )
    assert result.size == 0.5
    assert result.mode == "static_micro_risk"
