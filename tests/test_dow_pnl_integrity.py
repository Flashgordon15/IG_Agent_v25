"""DOW GBP PnL integrity — marks, UPL, and untrusted-entry guards."""

from __future__ import annotations

from execution.position_pnl_gbp import pnl_gbp_for_open_row


DOW = "IX.D.DOW.IFM.IP"


def test_dow_buy_unrealized_from_ig_marks():
    # DOW point_value=2.0 USD; BUY entry 45000, bid 44990 → −20 pts * 0.5 * 2 USD
    gbp = pnl_gbp_for_open_row(
        epic=DOW,
        direction="BUY",
        entry_level=45000.0,
        size=0.5,
        bid=44990.0,
        offer=44992.0,
        currency="USD",
    )
    assert gbp is not None
    assert gbp < 0
    # Rough band: ~−£8 to −£20 depending on FX — must be materially negative
    assert gbp > -40.0


def test_dow_prefers_broker_upl_when_present():
    gbp = pnl_gbp_for_open_row(
        epic=DOW,
        direction="BUY",
        entry_level=45000.0,
        size=0.5,
        upl=-122.35,
        bid=44900.0,
        offer=44902.0,
        currency="GBP",
    )
    assert gbp is not None
    assert abs(gbp + 122.35) < 0.01


def test_dow_rejects_zero_entry_even_with_upl():
    gbp = pnl_gbp_for_open_row(
        epic=DOW,
        direction="BUY",
        entry_level=0.0,
        size=0.5,
        upl=-122.35,
        bid=44900.0,
        offer=44902.0,
        currency="GBP",
    )
    assert gbp is None


def test_dow_rejects_yahoo_scale_mid():
    gbp = pnl_gbp_for_open_row(
        epic=DOW,
        direction="BUY",
        entry_level=45000.0,
        size=0.5,
        bid=99.0,
        offer=100.0,
        currency="USD",
    )
    assert gbp is None


def test_dow_sell_profit_from_marks():
    gbp = pnl_gbp_for_open_row(
        epic=DOW,
        direction="SELL",
        entry_level=45000.0,
        size=0.5,
        bid=44980.0,
        offer=44982.0,
        currency="USD",
    )
    assert gbp is not None
    assert gbp > 0
