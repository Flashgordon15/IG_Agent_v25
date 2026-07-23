"""Position P&L from IG marks."""

from __future__ import annotations

from execution.position_pnl_gbp import pnl_gbp_for_open_row


def test_pnl_from_ig_bid_offer_gold():
    gbp = pnl_gbp_for_open_row(
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        entry_level=4146.0,
        size=10.0,
        bid=4145.5,
        offer=4146.0,
        currency="USD",
    )
    assert gbp is not None
    assert gbp < 0


def test_pnl_rejects_yahoo_scale_mismatch():
    gbp = pnl_gbp_for_open_row(
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        entry_level=4146.0,
        size=10.0,
        bid=99.0,
        offer=100.0,
        currency="USD",
    )
    assert gbp is None
