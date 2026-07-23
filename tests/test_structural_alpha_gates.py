"""Asymmetric spread elasticity, leader-follower proxy, 3:1 R:R scaling."""

from __future__ import annotations

import time

import pytest

from diagnostics.fill_rate_monitor import (
    get_fill_rate_monitor,
    reset_fill_rate_monitor_for_tests,
)
from execution.adaptive_atr_bracket import resolve_adaptive_entry_bracket
from execution.grok_macro_bias import reset_grok_macro_bias_cache_for_tests
from execution.leader_follower_gate import (
    evaluate_leader_follower_gate,
    observe_leader_proxy_mid,
    reset_leader_follower_for_tests,
)
from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_decision
from execution.spread_elasticity import (
    observe_spread,
    reset_spread_elasticity_for_tests,
    spread_elasticity_state,
)


@pytest.fixture(autouse=True)
def _isolate_live_veto(monkeypatch):
    """Live desk may hold VETO on disk — isolate non-veto gate tests."""
    monkeypatch.setenv("IG_GROK_MACRO_BIAS", "NEUTRAL")
    reset_grok_macro_bias_cache_for_tests()
    yield
    reset_grok_macro_bias_cache_for_tests()


CFG = {
    "grok_macro_bias": "NEUTRAL",
    "pre_entry_regime_veto": {
        "enabled": True,
        "max_spread_pct": 0.01,  # loose hard cap so elasticity can fire
        "max_spread_pts": 0,
    },
    "obi_filter": {
        "enabled": True,
        "min_abs_ratio": 0.15,
        "require_align": False,
        "fail_closed_on_neutral": False,
    },
    "spread_elasticity": {"enabled": True, "elasticity_mult": 1.5},
    "leader_follower": {
        "enabled": True,
        "lookback_ticks": 4,
        "momentum_eps": 1e-12,
        "shadow_proxy": True,
        "divergence_guard": False,
        "leader_map": {"IX.D.DOW.IFM.IP": "PROXY.US30"},
    },
    "micro_risk": {
        "risk_per_trade_gbp": 4.0,
        "target_r_multiple": 2.0,
        "min_profit_target_pts": 1.0,
        "max_loss_cap_pts": 4.0,
        "virtual_stop_ceiling_pts": 4.0,
    },
    "volatility_bracket": {
        "enabled": True,
        "size_scale_vol_ratio_ceil": 1.35,
        "size_scale_floor": 0.35,
        "elevated_vol_reward_risk": 3.0,
        "fill_rate_size_scale_threshold": 0.50,
        "fill_rate_size_scale": 0.80,
    },
}


def setup_function():
    reset_spread_elasticity_for_tests()
    reset_leader_follower_for_tests()
    reset_fill_rate_monitor_for_tests()
    reset_grok_macro_bias_cache_for_tests()


def test_spread_elasticity_flags_wide_vs_ma():
    epic = "IX.D.DOW.IFM.IP"
    base = time.time() - 50.0
    # Build baseline with tight spread=2 (recent timestamps so 1h window keeps them)
    for i in range(40):
        observe_spread(epic, 100.0, 102.0, now=base + i)
    # Current streaming spread=4 → 2.0x MA
    st = spread_elasticity_state(epic, 100.0, 104.0, elasticity_mult=1.5)
    assert st.elastic is True
    assert st.ratio >= 1.5


def test_regime_routes_working_order_when_elastic(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_obi_entry_filter",
        lambda *a, **k: (True, "obi_ok", 0.2),
    )
    # FTSE profile max_spread_pts=4.5 — allows elastic width DOW's 3.0 hard cap would block.
    epic = "IX.D.FTSE.IFM.IP"
    base = time.time() - 50.0
    for i in range(40):
        observe_spread(epic, 8000.0, 8002.0, now=base + i)
    # MA≈2; spread=3.5 → ratio 1.75 > 1.5 elasticity, still ≤ FTSE 4.5 pt cap
    d = evaluate_pre_entry_regime_decision(
        epic, "BUY", bid=8000.0, offer=8003.5, cfg=CFG
    )
    assert d.allowed is True
    assert d.entry_route == "WORKING_ORDER"
    assert d.touch_level is not None and d.touch_level > 0
    assert "working_order" in d.reason


def test_leader_follower_vetoes_buy_on_negative_momentum():
    reset_leader_follower_for_tests()
    base = time.time() - 10.0
    # Declining proxy mids
    for i, mid in enumerate([100.0, 99.8, 99.5, 99.2, 98.8]):
        observe_leader_proxy_mid("PROXY.US30", mid, now=base + i)
    ok, reason = evaluate_leader_follower_gate(
        "IX.D.DOW.IFM.IP",
        "BUY",
        bid=98.7,
        offer=98.9,
        cfg=CFG,
    )
    assert ok is False
    assert "buy_veto" in reason


def test_asymmetric_rr_keeps_sl_tight_scales_tp(monkeypatch):
    monkeypatch.setattr(
        "execution.adaptive_atr_bracket._resolve_atrs",
        lambda epic: (30.0, 10.0),  # vol_ratio = 3.0 elevated
    )
    result = resolve_adaptive_entry_bracket(
        "IX.D.DOW.IFM.IP", "BUY", 0.5, CFG, entry=45000.0
    )
    assert result.mode == "asymmetric_rr_elevated_vol"
    assert result.sl_pts <= 4.0 + 1e-9  # max_loss_cap
    assert result.tp_pts >= result.sl_pts * 3.0 - 1e-6


def test_grok_macro_veto_fail_closes(monkeypatch):
    monkeypatch.setattr(
        "execution.grok_macro_bias.resolve_grok_macro_bias",
        lambda cfg=None: "VETO",
    )
    d = evaluate_pre_entry_regime_decision(
        "IX.D.DOW.IFM.IP", "BUY", bid=50000.0, offer=50002.0, cfg=CFG
    )
    assert d.allowed is False
    assert d.entry_route == "BLOCK"
    assert "grok_macro_bias_VETO" in d.reason


def test_divergence_arms_5s_veto():
    reset_leader_follower_for_tests()
    epic = "IX.D.DOW.IFM.IP"
    cfg = {
        **CFG,
        "leader_follower": {
            "enabled": True,
            "lookback_ticks": 4,
            "momentum_eps": 1e-12,
            "shadow_proxy": False,
            "divergence_guard": True,
            "divergence_streak_limit": 3,
            "divergence_veto_sec": 5.0,
            "leader_map": {epic: "PROXY.US30"},
        },
    }
    # IG climbing, proxy falling — 4 consecutive divergent ticks (>3).
    # Use SELL so declining proxy momentum does not trip the BUY veto first.
    base = 100.0
    ok, reason = True, ""
    for i in range(5):
        ig = base + i * 0.5
        px = base - i * 0.5
        observe_leader_proxy_mid("PROXY.US30", px)
        ok, reason = evaluate_leader_follower_gate(
            epic, "SELL", bid=ig - 0.1, offer=ig + 0.1, cfg=cfg
        )
    assert ok is False
    assert "divergence_veto" in reason


def test_fill_rate_haircut_scales_size_keeps_rr(monkeypatch):
    monkeypatch.setattr(
        "execution.adaptive_atr_bracket._resolve_atrs",
        lambda epic: (30.0, 10.0),
    )
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    # 20 outcomes @ 40% fill → below 50% threshold
    for _ in range(8):
        mon.record_fill()
    for _ in range(12):
        mon.record_slippage_reject("slip")
    low = resolve_adaptive_entry_bracket(
        "IX.D.DOW.IFM.IP", "BUY", 0.5, CFG, entry=45000.0
    )
    mon.reset()
    for _ in range(20):
        mon.record_fill()
    high = resolve_adaptive_entry_bracket(
        "IX.D.DOW.IFM.IP", "BUY", 0.5, CFG, entry=45000.0
    )
    assert low.size < high.size
    assert low.size <= high.size * 0.80 + 1e-9
    # 3:1 R:R preserved under haircut
    assert low.tp_pts >= low.sl_pts * 3.0 - 1e-6
