"""In-memory Shared Memory Context Matrix — hollow veto + parameter assignment."""

from __future__ import annotations

from system.memory_context import (
    QUOTE_FRESHNESS_SEC,
    MemoryContext,
    is_hollow_ghost_row,
    reset_memory_context_for_tests,
)


def test_hollow_ghost_rows_hard_vetoed():
    """Case A: entry==0 or pnl_gbp null dropped; critical alarms retained."""
    reset_memory_context_for_tests()
    ctx = MemoryContext()
    rows = [
        {
            "deal_id": "GHOST1",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "BUY",
            "size": 0.5,
            "entry": 0.0,
            "pnl_gbp": None,
            "source": "trade_support_overlay",
        },
        {
            "deal_id": "REAL1",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "BUY",
            "size": 0.5,
            "entry": 52000.0,
            "pnl_gbp": 1.25,
            "soft_loss_gbp": 2.2,
            "trail_floor_gbp": 0.8,
            "target_gbp": 7.0,
            "source": "broker_snapshot",
        },
        {
            "deal_id": "ALARM1",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "BUY",
            "size": 0.0,
            "entry": 0.0,
            "pnl_gbp": None,
            "critical_alarm": True,
            "flatten_failed": True,
            "source": "trade_support_overlay",
        },
    ]
    assert is_hollow_ghost_row(rows[0]) is True
    assert is_hollow_ghost_row(rows[1]) is False
    assert is_hollow_ghost_row(rows[2]) is False
    # Overlay with entry but null pnl is still a ghost
    assert (
        is_hollow_ghost_row(
            {
                "deal_id": "X",
                "entry": 100.0,
                "pnl_gbp": None,
                "source": "trade_support_overlay",
            }
        )
        is True
    )

    verified = ctx.sync_open_rows(rows, atr_by_epic={"IX.D.DOW.IFM.IP": 14.0})
    ids = {r["deal_id"] for r in verified}
    assert "GHOST1" not in ids
    assert "REAL1" in ids
    assert "ALARM1" in ids
    assert ctx.count() == 2
    real = next(m for m in ctx.open_positions() if m.deal_id == "REAL1")
    assert real.soft_loss_gbp == 2.2
    assert real.trail_floor_gbp == 0.8
    assert real.take_profit_level is not None
    assert real.take_profit_level > real.entry  # 3.5× ATR BUY


def test_quote_freshness_500ms_budget():
    """Case B: explicit 500ms budget still fail-closes (WS sniper path)."""
    reset_memory_context_for_tests()
    ctx = MemoryContext()
    assert QUOTE_FRESHNESS_SEC == 0.5
    assert ctx.set_quote_freshness(0.2, budget_sec=0.5) is True
    assert ctx.quotes_fresh() is True
    assert ctx.set_quote_freshness(0.51, budget_sec=0.5) is False
    assert ctx.quotes_fresh() is False
    snap = ctx.snapshot()
    assert snap["quote_freshness_budget_sec"] == 0.5
    assert snap["quotes_fresh"] is False


def test_settle_gbp_and_win_loss_classification():
    """Case C: settlement packet → true GBP; CANCELLED never survives cash settle."""
    from system.pnl_math import classify_result_gbp, settle_gbp_from_ig

    gbp = settle_gbp_from_ig(
        profit_and_loss=None,
        ig_pnl_currency=None,
        pnl_points=10.0,
        contract_size=0.5,
        point_value=1.0,
    )
    assert gbp == 5.0
    assert classify_result_gbp(5.0) == "WIN"
    assert classify_result_gbp(-1.2) == "LOSS"
    assert classify_result_gbp(0.005) == "BREAKEVEN"
    from_ig = settle_gbp_from_ig(profit_and_loss=-3.5)
    assert from_ig == -3.5
