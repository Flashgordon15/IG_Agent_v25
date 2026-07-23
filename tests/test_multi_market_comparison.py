"""Multi-market comparison harness — DOW / FTSE / Gold / EURUSD simultaneous breakouts.

Verifies RuntimeContext spread caps, trail crawl by point_multiplier, dynamic
entry gates, and forex maxSlippage fractional-pip adaptation.
"""

from __future__ import annotations

import pytest

from execution.asymmetric_ioc_router import (
    adapt_max_slippage_for_ig,
    build_ig_otc_market_payload,
    compute_max_slippage,
)
from execution.entry_gate_hardening import evaluate_spread_hard_veto
from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_veto
from system.memory_context import (
    EPIC_DOW,
    EPIC_EURUSD,
    EPIC_FTSE,
    EPIC_GOLD,
    ALIAS_EURUSD,
    ALIAS_FTSE,
    ALIAS_GOLD,
    get_runtime_context,
    reset_runtime_context_for_tests,
    resolve_asset_profile,
)


# Identical high-velocity breakout envelope applied to every instrument.
BREAKOUT = {
    "direction": "BUY",
    "velocity": 180.0,
    "z_score": 1.85,
    "trail_pts": 2.0,
    "size": 0.5,
}


INSTRUMENTS = (
    {
        "key": "IX.D.DOW.IFM.IP",
        "epic": EPIC_DOW,
        "alias": "IX.D.DOW.IFM.IP",
        "max_spread_pts": 3.0,
        "point_multiplier": 1.0,
        "is_forex": False,
        "bid": 52000.0,
        "tight_offer": 52002.0,  # spread 2.0 ≤ 3.0
        "wide_offer": 52004.0,  # spread 4.0 > 3.0
    },
    {
        "key": ALIAS_FTSE,
        "epic": EPIC_FTSE,
        "alias": ALIAS_FTSE,
        "max_spread_pts": 4.5,
        "point_multiplier": 2.0,
        "is_forex": False,
        "bid": 8200.0,
        "tight_offer": 8204.0,  # spread 4.0 ≤ 4.5
        "wide_offer": 8206.0,  # spread 6.0 > 4.5
    },
    {
        "key": ALIAS_GOLD,
        "epic": EPIC_GOLD,
        "alias": ALIAS_GOLD,
        "max_spread_pts": 40.0,
        "point_multiplier": 10.0,
        "is_forex": False,
        "bid": 2350.0,
        "tight_offer": 2380.0,  # spread 30 ≤ 40
        "wide_offer": 2395.0,  # spread 45 > 40
    },
    {
        "key": ALIAS_EURUSD,
        "epic": EPIC_EURUSD,
        "alias": ALIAS_EURUSD,
        "max_spread_pts": 2.0,
        "point_multiplier": 10000.0,
        "is_forex": True,
        "bid": 1.08500,
        "tight_offer": 1.08510,  # 1.0 pip ≤ 2.0 pip cap
        "wide_offer": 1.08530,  # 3.0 pips > 2.0 pip cap
    },
)


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    reset_runtime_context_for_tests()
    monkeypatch.setenv("IG_GROK_MACRO_BIAS", "NEUTRAL")
    monkeypatch.setattr(
        "execution.grok_macro_bias.resolve_grok_macro_bias",
        lambda cfg=None: "NEUTRAL",
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_obi_entry_filter",
        lambda *a, **k: (True, "obi_ok", 0.2),
    )
    yield
    reset_runtime_context_for_tests()


def test_four_market_spread_caps_simultaneous_breakout():
    """Case 1 — each asset reads its unique RuntimeContext spread cap."""
    rt = get_runtime_context()
    for inst in INSTRUMENTS:
        prof = rt.profile_for(inst["alias"])
        assert prof.epic == inst["epic"]
        assert prof.max_spread_pts == pytest.approx(inst["max_spread_pts"])
        assert prof.point_multiplier == pytest.approx(inst["point_multiplier"])
        assert prof.is_forex is inst["is_forex"]

        tight = float(inst["tight_offer"]) - float(inst["bid"])
        assert rt.spread_allowed(inst["epic"], tight) is True

        wide = float(inst["wide_offer"]) - float(inst["bid"])
        assert rt.spread_allowed(inst["epic"], wide) is False


def test_four_market_trail_crawl_by_point_multiplier():
    """Case 2 — trailing stop crawl scales with contract point multipliers."""
    rt = get_runtime_context()
    trail_pts = float(BREAKOUT["trail_pts"])
    expected = {
        EPIC_DOW: trail_pts * 1.0,  # index: pts × multiplier
        EPIC_FTSE: trail_pts * 2.0,
        EPIC_GOLD: trail_pts * 10.0,
        EPIC_EURUSD: trail_pts / 10000.0,  # forex: pips → price
    }
    for inst in INSTRUMENTS:
        delta = rt.trail_price_delta(inst["epic"], trail_pts)
        assert delta == pytest.approx(expected[inst["epic"]], rel=1e-9)
        # GBP at size 0.5: indices/commodities use trail_pts × size
        gbp = rt.trail_gbp(inst["epic"], trail_pts, BREAKOUT["size"])
        if inst["is_forex"]:
            assert gbp == pytest.approx(trail_pts * float(BREAKOUT["size"]), rel=1e-9)
        else:
            assert gbp == pytest.approx(trail_pts * float(BREAKOUT["size"]), rel=1e-9)


def test_four_market_entry_gates_dynamic_spread():
    """Case 3 — hot-path gates use memory lookup, not hardcoded 3.0."""
    from types import SimpleNamespace

    for inst in INSTRUMENTS:
        epic = inst["epic"]
        # Tight book passes
        ok, reason = evaluate_pre_entry_regime_veto(
            epic,
            BREAKOUT["direction"],
            bid=inst["bid"],
            offer=inst["tight_offer"],
            cfg={
                "pre_entry_regime_veto": {"enabled": True, "enforce_spread_pct": False},
                "obi_filter": {"enabled": False},
                "spread_elasticity": {"enabled": False},
                "leader_follower": {"enabled": False},
                "grok_macro_bias": "NEUTRAL",
            },
        )
        assert ok is True, f"{epic} tight failed: {reason}"

        ok_w, reason_w = evaluate_pre_entry_regime_veto(
            epic,
            BREAKOUT["direction"],
            bid=inst["bid"],
            offer=inst["wide_offer"],
            cfg={
                "pre_entry_regime_veto": {
                    "enabled": True,
                    "enforce_spread_pct": False,
                },
                "obi_filter": {"enabled": False},
                "spread_elasticity": {"enabled": False},
                "leader_follower": {"enabled": False},
                "grok_macro_bias": "NEUTRAL",
            },
        )
        assert ok_w is False, f"{epic} wide should block"
        assert "spread_pts" in reason_w

        # Confirm gate hardening resolves same profile cap (FX → pips)
        wide_price = float(inst["wide_offer"]) - float(inst["bid"])
        ok_g, reason_g, measured = evaluate_spread_hard_veto(
            epic,
            cfg={"feed_quality": {"enabled": True, "spread_hard_veto": True}},
            quote=SimpleNamespace(
                bid=inst["bid"],
                offer=inst["wide_offer"],
                mid=(inst["bid"] + inst["wide_offer"]) / 2,
            ),
        )
        assert ok_g is False
        assert "spread_hard_veto" in reason_g
        prof = resolve_asset_profile(epic)
        assert prof.max_spread_pts == pytest.approx(inst["max_spread_pts"])
        if prof.is_forex:
            assert measured == pytest.approx(wide_price * prof.point_multiplier)
        else:
            assert measured == pytest.approx(wide_price)


def test_four_market_forex_ioc_slippage_and_index_integers():
    """Case 4 — forex maxSlippage is fractional pips; indices stay integers."""
    # Simultaneous breakout books
    books = {
        EPIC_DOW: (52000.0, 52002.0),  # spread 2 → slip 1 int
        EPIC_FTSE: (8200.0, 8204.0),  # spread 4 → slip 2 int
        EPIC_GOLD: (2350.0, 2370.0),  # spread 20 → slip 10 int
        EPIC_EURUSD: (1.08500, 1.08520),  # spread 0.00020 → 1.0 pip @ 0.5x
    }
    for epic, (bid, offer) in books.items():
        slip = compute_max_slippage(bid, offer, slip_mult=0.5, epic=epic)
        adapted = adapt_max_slippage_for_ig(epic, slip)
        payload = build_ig_otc_market_payload(
            epic=epic,
            direction="BUY",
            size=0.5,
            stop_distance=4.0,
            max_slippage=slip,
        )
        prof = resolve_asset_profile(epic)
        if prof.is_forex:
            assert isinstance(adapted, float) or (
                isinstance(adapted, (int, float)) and float(adapted) == adapted
            )
            assert float(payload["maxSlippage"]) == pytest.approx(1.0, abs=0.05)
            assert float(payload["maxSlippage"]) == pytest.approx(float(adapted))
        else:
            assert isinstance(payload["maxSlippage"], int)
            assert payload["maxSlippage"] == int(adapted)
            assert payload["maxSlippage"] >= 1


def test_edge_extreme_spread_widening_blocks_all_four_markets():
    """Edge — flash spread widen beyond each asset's RuntimeContext cap → veto."""
    from types import SimpleNamespace

    flash = {
        EPIC_DOW: (52000.0, 52020.0),  # 20 pts >> 3
        EPIC_FTSE: (8200.0, 8220.0),  # 20 pts >> 4.5
        EPIC_GOLD: (2350.0, 2450.0),  # 100 pts >> 40
        EPIC_EURUSD: (1.08500, 1.08600),  # 10 pips >> 2
    }
    for epic, (bid, offer) in flash.items():
        ok, reason = evaluate_pre_entry_regime_veto(
            epic,
            "BUY",
            bid=bid,
            offer=offer,
            cfg={
                "pre_entry_regime_veto": {
                    "enabled": True,
                    "enforce_spread_pct": False,
                    "require_trending_regime": False,
                },
                "obi_filter": {"enabled": False},
                "spread_elasticity": {"enabled": False},
                "leader_follower": {"enabled": False},
                "grok_macro_bias": "NEUTRAL",
            },
        )
        assert ok is False, f"{epic} flash widen should block"
        assert "spread" in reason
        ok_g, reason_g, _ = evaluate_spread_hard_veto(
            epic,
            cfg={"feed_quality": {"enabled": True, "spread_hard_veto": True}},
            quote=SimpleNamespace(bid=bid, offer=offer, mid=(bid + offer) / 2),
        )
        assert ok_g is False
        assert "spread_hard_veto" in reason_g


def test_edge_flash_crash_telemetry_dropout_regime_warmup_fail_open():
    """Edge — regime telemetry dropout (empty markets) warmups fail-open; CHOP blocks."""
    from execution.pre_entry_regime_veto import (
        evaluate_trending_regime_gate,
        _resolve_regime_label,
    )

    # Empty / missing label → warmup fail-open (no silent freeze on Mini boot)
    ok, reason = evaluate_trending_regime_gate(
        EPIC_DOW,
        cfg={"pre_entry_regime_veto": {"require_trending_regime": True}},
    )
    label = _resolve_regime_label(EPIC_DOW)
    if not label:
        assert ok is True
        assert "warmup" in reason
    # Explicit CHOP / RANGE_BOUND / NEUTRAL must hard-block multi-market entries
    import execution.pre_entry_regime_veto as prv

    monkey_labels = ("CHOP", "RANGE_BOUND", "NEUTRAL", "MEAN_REVERSION", "STAGNANT_DZ")
    for bad in monkey_labels:
        orig = prv._resolve_regime_label
        prv._resolve_regime_label = lambda epic, _b=bad: _b  # type: ignore[assignment]
        try:
            ok_b, reason_b = evaluate_trending_regime_gate(
                EPIC_FTSE,
                cfg={"pre_entry_regime_veto": {"require_trending_regime": True}},
            )
            assert ok_b is False, f"{bad} must block"
            assert "not_trending" in reason_b
        finally:
            prv._resolve_regime_label = orig  # type: ignore[assignment]


def test_edge_rolling_wr_size_contraction_and_gold_obi_threshold():
    """Edge — degraded 10-trade WR → fractional size curve; Gold OBI threshold tighter than DOW."""
    from execution.asymmetric_ioc_router import (
        apply_wr_size_contraction,
        fractional_expectancy_multiplier,
        note_closed_trade_outcome,
        reset_asymmetric_router_state_for_tests,
    )

    reset_asymmetric_router_state_for_tests()
    assert fractional_expectancy_multiplier() == pytest.approx(1.0)
    # 1 win + 4 losses → WR=0.20 < 0.50 → fractional contraction
    for pnl in (2.0, -1.0, -1.0, -1.0, -1.0):
        note_closed_trade_outcome(pnl)
    mult = fractional_expectancy_multiplier()
    assert 0.35 < mult < 1.0
    assert apply_wr_size_contraction(0.5) == pytest.approx(0.5 * mult)
    # With epic: floor at IG min lot (DOW 0.5) — cannot transmit under min
    assert apply_wr_size_contraction(0.5, epic=EPIC_DOW) == pytest.approx(0.5)
    reset_asymmetric_router_state_for_tests()

    gold = resolve_asset_profile(EPIC_GOLD)
    dow = resolve_asset_profile(EPIC_DOW)
    eurusd = resolve_asset_profile(EPIC_EURUSD)
    assert gold.obi_threshold >= 0.28
    assert dow.obi_threshold >= 0.22
    assert eurusd.obi_threshold >= 0.20
    assert gold.obi_threshold > dow.obi_threshold


def test_edge_soft_loss_widens_in_chop_vs_spread_harvest():
    """Edge — chop regime widens soft_loss toward hard-cap ceiling (not harvested)."""
    from execution.open_position_rules import spread_adjusted_soft_loss_gbp
    import execution.pre_entry_regime_veto as prv

    orig = prv._resolve_regime_label
    prv._resolve_regime_label = lambda epic: "CHOP"  # type: ignore[assignment]
    try:
        widened = spread_adjusted_soft_loss_gbp(
            epic=EPIC_DOW,
            size=0.5,
            soft_loss_gbp=2.2,
            loss_cap_gbp=4.0,
            trail_trigger_gbp=1.5,
            peak_profit_gbp=0.0,
        )
        assert widened > 2.2
        assert widened <= 4.0 * 0.90 + 1e-9
    finally:
        prv._resolve_regime_label = orig  # type: ignore[assignment]
