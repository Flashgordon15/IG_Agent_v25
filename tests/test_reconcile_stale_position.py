"""Stale position reconciler — extract + synthetic PnL (no live IG)."""

from __future__ import annotations

from execution.reconcile_stale_position import (
    extract_broker_fields,
    extract_from_activity_or_txn,
    synthetic_pnl_gbp,
)


def test_extract_broker_fields_from_positions_payload():
    item = {
        "position": {
            "dealId": "DIAAAAXY5H2RZAR",
            "direction": "BUY",
            "size": 0.5,
            "level": 52146.5,
            "currency": "GBP",
            "profitAndLoss": -12.5,
        },
        "market": {
            "epic": "IX.D.DOW.IFM.IP",
            "bid": 52140.0,
            "offer": 52150.0,
            "currency": "GBP",
        },
    }
    fields = extract_broker_fields(item)
    assert fields["deal_id"] == "DIAAAAXY5H2RZAR"
    assert fields["entry"] == 52146.5
    assert fields["currency"] == "GBP"
    assert fields["epic"] == "IX.D.DOW.IFM.IP"
    assert fields["size"] == 0.5


def test_extract_from_activity_journal():
    rows = [
        {
            "dealId": "DIAAAAXY5H2RZAR",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "BUY",
            "level": 52100.0,
            "size": 0.5,
            "currency": "GBP",
        }
    ]
    hit = extract_from_activity_or_txn(rows, "DIAAAAXY5H2RZAR")
    assert hit is not None
    assert hit["entry"] == 52100.0
    assert hit["currency"] == "GBP"


def test_synthetic_pnl_buy_when_marks_present():
    # BUY 0.5 £/pt @ 52100 vs bid 52120 → +20 pts → +£10 (spreadbet size=£/pt)
    pnl = synthetic_pnl_gbp(
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        entry=52100.0,
        size=0.5,
        bid=52120.0,
        offer=52122.0,
        currency="GBP",
    )
    assert pnl is not None
    assert abs(pnl - 10.0) < 1.0  # allow FX/spec scaling tolerance
