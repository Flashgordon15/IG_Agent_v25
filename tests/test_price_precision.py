"""Tests — broker price precision and position deduplication."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.price_precision import parse_broker_price
from trading.open_position_view import (
    broker_pnl_is_authoritative,
    extract_broker_profit_and_loss,
    position_map_from_rows,
)


class PricePrecisionTests(unittest.TestCase):
    def test_fx_preserves_five_decimals(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        self.assertEqual(parse_broker_price("1.16125", epic=epic), 1.16125)
        self.assertEqual(parse_broker_price(1.161249, epic=epic), 1.16125)

    def test_no_integer_truncation(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        self.assertNotEqual(parse_broker_price("1.16125", epic=epic), 1.0)


class BrokerPnlTests(unittest.TestCase):
    def test_profit_and_loss_usd_row(self) -> None:
        row = {
            "dealId": "DIAAAAXNM2VYUAN",
            "epic": "CS.D.EURUSD.CFD.IP",
            "profitAndLoss": -84.0,
            "currency": "USD",
            "level": 1.16125,
        }
        self.assertTrue(broker_pnl_is_authoritative(row))
        upl, ccy = extract_broker_profit_and_loss(row)
        self.assertEqual(upl, -84.0)
        self.assertEqual(ccy, "USD")


class PositionMapTests(unittest.TestCase):
    def test_dedup_by_deal_id(self) -> None:
        rows = [
            {"dealId": "A1", "epic": "CS.D.EURUSD.CFD.IP", "pnl_gbp": 1.0},
            {"dealId": "A1", "epic": "CS.D.EURUSD.CFD.IP", "pnl_gbp": 2.0},
            {"deal_id": "B2", "epic": "IX.D.DOW.IFM.IP", "pnl_gbp": -3.0},
        ]
        pmap = position_map_from_rows(rows)
        self.assertEqual(set(pmap.keys()), {"A1", "B2"})
        self.assertEqual(pmap["A1"]["pnl_gbp"], 2.0)


if __name__ == "__main__":
    unittest.main()
