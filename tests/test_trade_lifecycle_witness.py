"""Unit tests for loss autopsy policy / APP|LOGIC|UNKNOWN classifiers."""

from __future__ import annotations

from diagnostics.trade_lifecycle_witness import (
    DOW_EPIC,
    NIKKEI_EPIC,
    PolicyContext,
    TradeLifecycle,
    classify_loss,
    classify_policy_breaches,
    infer_epic_from_market_price,
)


def _lc(**kwargs) -> TradeLifecycle:
    base = dict(
        deal_id="DIAAAAXTEST",
        pnl_gbp=-4.0,
        account_id="Z6BAH3",
        product_type="SPREADBET",
        engine_origin="MACRO_SENTINEL",
        style="macro",
        epic="IX.D.DOW.IFM.IP",
        hold_sec=200.0,
        ml_score_at_entry=0.72,
        market_regime="TRENDING",
        exit_reason="soft_loss",
    )
    base.update(kwargs)
    return TradeLifecycle(**base)


def test_excluded_nikkei_is_app_breach():
    ctx = PolicyContext(
        exclude_from_hot_path=["IX.D.NIKKEI.IFM.IP"],
        path_a_claimed=True,
        sb_disable_instant_micro=True,
        sb_disable_core_b_micro=True,
    )
    lc = _lc(epic="IX.D.NIKKEI.IFM.IP", hold_sec=3.0, engine_origin="broker_attached", style="supervised_exit")
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    codes = {b.code for b in lc.policy_breaches}
    assert "EXCLUDED_EPIC" in codes
    klass, reason = classify_loss(lc, ctx)
    assert klass == "APP"
    assert "EXCLUDED_EPIC" in reason


def test_macro_short_hold_is_app():
    ctx = PolicyContext(path_a_claimed=True, micro_hold_breach_sec=60.0)
    lc = _lc(hold_sec=6.0, engine_origin="MACRO_SENTINEL", style="macro")
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    assert any(b.code == "HOLD_LT_MACRO_INTENT" and b.severity == "fail" for b in lc.policy_breaches)
    klass, _ = classify_loss(lc, ctx)
    assert klass == "APP"


def test_logic_soft_loss_on_dow():
    ctx = PolicyContext(
        exclude_from_hot_path=["IX.D.NIKKEI.IFM.IP"],
        path_a_claimed=True,
        a2_cfd_paused=False,
    )
    lc = _lc(
        epic="IX.D.DOW.IFM.IP",
        hold_sec=220.0,
        engine_origin="MACRO_SENTINEL",
        exit_reason="open_position_actions:soft_loss breach",
        ml_score_at_entry=0.71,
        market_regime="TRENDING",
    )
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    # may still have no hard APP codes
    hard = {b.code for b in lc.policy_breaches if b.severity == "fail"}
    assert "EXCLUDED_EPIC" not in hard
    assert "HOLD_LT_MACRO_INTENT" not in hard
    klass, reason = classify_loss(lc, ctx)
    assert klass == "LOGIC"
    assert "exit" in reason.lower() or "strategy" in reason.lower() or "policy path" in reason.lower()


def test_missing_stamps_unknown():
    """Non-broker close with no stamps → UNKNOWN (cannot certify path)."""
    ctx = PolicyContext(path_a_claimed=True, a2_cfd_paused=False)
    lc = _lc(
        epic="IX.D.DOW.IFM.IP",
        hold_sec=None,
        ml_score_at_entry=None,
        market_regime=None,
        engine_origin="MACRO_SENTINEL",
        exit_reason="",
        style="macro",
        pnl_gbp=-1.0,
    )
    lc.evidence_gaps = ["missing_ml_score_at_entry", "missing_hold_sec"]
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    klass, reason = classify_loss(lc, ctx)
    assert klass == "UNKNOWN"
    assert "evidence gap" in reason.lower()


def test_broker_close_missing_stamps_is_supervision_gap_app():
    """Broker-attached close is APP SUPERVISION_GAP even when stamps are blank."""
    ctx = PolicyContext(path_a_claimed=True, a2_cfd_paused=False)
    lc = _lc(
        epic="IX.D.DOW.IFM.IP",
        hold_sec=None,
        ml_score_at_entry=None,
        market_regime="UNKNOWN",
        engine_origin="broker_attached",
        exit_reason="broker_attached",
        style="supervised_exit",
        pnl_gbp=-1.5,  # within soft-loss overrun → not RISK_STACK
    )
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    assert any(b.code == "SUPERVISION_GAP" for b in lc.policy_breaches)
    klass, reason = classify_loss(lc, ctx)
    assert klass == "APP"
    assert "SUPERVISION_GAP" in reason


def test_placeholder_regime_counts_as_missing():
    from diagnostics.stamp_provenance import is_placeholder_regime

    assert is_placeholder_regime(None)
    assert is_placeholder_regime("")
    assert is_placeholder_regime("UNKNOWN")
    assert not is_placeholder_regime("TREND")
    assert not is_placeholder_regime("CHOP")

def test_cfd_while_a2_paused_app():
    ctx = PolicyContext(a2_cfd_paused=True, path_a_claimed=True)
    lc = _lc(
        account_id="Z6BAH4",
        product_type="CFD",
        epic="IX.D.DOW.IFM.IP",
        engine_origin="QUANT_SNIPER",
        style="scalp",
        hold_sec=40.0,
    )
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    assert any(b.code == "CFD_ENTRY_WHILE_A2_PAUSED" for b in lc.policy_breaches)
    klass, _ = classify_loss(lc, ctx)
    assert klass == "APP"


def test_instant_micro_on_sb_while_disabled():
    ctx = PolicyContext(
        path_a_claimed=True,
        sb_disable_instant_micro=True,
        sb_disable_core_b_micro=True,
    )
    lc = _lc(
        account_id="Z6BAH3",
        engine_origin="ENGINE_B_MICRO_SCALPER",
        style="scalp",
        hold_sec=12.0,
        epic="IX.D.DOW.IFM.IP",
    )
    lc.policy_breaches = classify_policy_breaches(lc, ctx)
    assert any(b.code == "INSTANT_MICRO_WHILE_DISABLED" for b in lc.policy_breaches)


def test_infer_epic_corrects_wall_street_nikkei_mislabels():
    epic, notes = infer_epic_from_market_price(
        epic=NIKKEI_EPIC,
        market="Wall Street",
        entry_price=51731.2,
    )
    assert epic == DOW_EPIC
    assert any("mislabelled" in n or "corrected" in n for n in notes)


def test_infer_epic_keeps_true_nikkei():
    epic, notes = infer_epic_from_market_price(
        epic=NIKKEI_EPIC,
        market="Japan 225",
        entry_price=64572.2,
    )
    assert epic == NIKKEI_EPIC
