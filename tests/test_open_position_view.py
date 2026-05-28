"""Tests for open position dashboard P&L mapping."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from trading.open_position_view import (
    enrich_positions_with_quote,
    normalize_sync_position,
    unrealized_from_quote,
)


class TestOpenPositionView(unittest.TestCase):
    def test_normalize_maps_ig_upl_to_pnl_gbp(self) -> None:
        row = normalize_sync_position(
            {
                "deal_id": "D1",
                "direction": "BUY",
                "level": 65000.0,
                "upl": 12.5,
                "size": 1.0,
                "stop_level": 64900.0,
                "limit_level": 65200.0,
            }
        )
        self.assertEqual(row["side"], "BUY")
        self.assertEqual(row["entry"], 65000.0)
        self.assertEqual(row["pnl_gbp"], 12.5)

    def test_enrich_keeps_ig_upl_when_present(self) -> None:
        quote = Quote(datetime(2026, 5, 27, 12, 0), 65100.0, 65107.0)
        base = [
            normalize_sync_position(
                {
                    "deal_id": "D1",
                    "direction": "BUY",
                    "level": 65000.0,
                    "upl": 5.0,
                    "size": 2.0,
                    "epic": "IX.D.NIKKEI.IFM.IP",
                }
            )
        ]
        out = enrich_positions_with_quote(
            base, quote, point_value_gbp=1.0, epic="IX.D.NIKKEI.IFM.IP"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["current"], 65100.0)
        self.assertEqual(out[0]["pnl_pts"], 100.0)
        self.assertEqual(out[0]["pnl_gbp"], 5.0)

    def test_enrich_quote_pnl_when_no_ig_upl(self) -> None:
        quote = Quote(datetime(2026, 5, 27, 12, 0), 65100.0, 65107.0)
        base = [
            normalize_sync_position(
                {
                    "deal_id": "D1",
                    "direction": "BUY",
                    "level": 65000.0,
                    "size": 2.0,
                    "epic": "IX.D.NIKKEI.IFM.IP",
                }
            )
        ]
        out = enrich_positions_with_quote(
            base, quote, point_value_gbp=1.0, epic="IX.D.NIKKEI.IFM.IP"
        )
        self.assertEqual(out[0]["pnl_gbp"], 200.0)

    def test_unrealized_sell_uses_offer(self) -> None:
        quote = Quote(datetime(2026, 5, 27, 12, 0), 65100.0, 65105.0)
        mark, pts, gbp = unrealized_from_quote(
            "SELL", 65100.0, 1.0, quote, point_value_gbp=1.0
        )
        self.assertEqual(mark, 65105.0)
        self.assertEqual(pts, -5.0)
        self.assertEqual(gbp, -5.0)


if __name__ == "__main__":
    unittest.main()
