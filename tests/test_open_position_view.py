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
    apply_display_daily_pnl,
    enrich_positions_with_quote,
    normalize_sync_position,
    sum_open_unrealized_gbp,
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

    def test_enrich_prefers_live_quote_over_stale_ig_upl(self) -> None:
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
        self.assertEqual(out[0]["pnl_gbp"], 200.0)

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

    def test_fx_unrealized_uses_pip_scale(self) -> None:
        quote = Quote(datetime(2026, 6, 11, 12, 0), 1.08510, 1.08512)
        mark, pts, gbp = unrealized_from_quote(
            "BUY",
            1.08500,
            10.0,
            quote,
            epic="CS.D.EURUSD.CFD.IP",
            point_value_gbp=1.0,
            currency="USD",
        )
        self.assertAlmostEqual(mark, 1.08510)
        self.assertAlmostEqual(pts, 1.0)
        self.assertAlmostEqual(gbp, 100.0 * 0.78, places=1)

    def test_fx_sub_pip_move_updates_pnl(self) -> None:
        quote = Quote(datetime(2026, 6, 11, 12, 0), 1.08501, 1.08503)
        mark, pts, gbp = unrealized_from_quote(
            "BUY",
            1.08500,
            10.0,
            quote,
            epic="CS.D.EURUSD.CFD.IP",
            point_value_gbp=1.0,
            currency="USD",
        )
        self.assertAlmostEqual(pts, 0.1)
        self.assertAlmostEqual(gbp, 10.0 * 0.78, places=1)

    def test_fx_size5_matches_ig_contract_value(self) -> None:
        quote = Quote(datetime(2026, 6, 12, 14, 0), 1.15712, 1.15716)
        mark, pts, gbp = unrealized_from_quote(
            "BUY",
            1.15673,
            5.0,
            quote,
            epic="CS.D.EURUSD.CFD.IP",
            currency="USD",
        )
        self.assertAlmostEqual(pts, 3.9, places=1)
        self.assertAlmostEqual(gbp, 3.9 * 5.0 * 10.0 * 0.78, places=0)

    def test_enrich_keeps_ig_upl_when_quote_scale_mismatch(self) -> None:
        quote = Quote(datetime(2026, 5, 27, 12, 0), 100.0, 100.5)
        base = [
            normalize_sync_position(
                {
                    "deal_id": "D1",
                    "direction": "BUY",
                    "level": 65000.0,
                    "upl": 8.0,
                    "size": 1.0,
                    "epic": "IX.D.NIKKEI.IFM.IP",
                }
            )
        ]
        out = enrich_positions_with_quote(
            base, quote, point_value_gbp=1.0, epic="IX.D.NIKKEI.IFM.IP"
        )
        self.assertEqual(out[0]["pnl_gbp"], 8.0)

    def test_apply_display_daily_pnl_is_idempotent(self) -> None:
        tick = {
            "realized_daily_pnl_gbp": 10.0,
            "daily_pnl_gbp": 10.0,
            "positions": [{"deal_id": "D1", "pnl_gbp": -3.25}],
        }
        apply_display_daily_pnl(tick)
        apply_display_daily_pnl(tick)
        self.assertEqual(tick["daily_pnl_gbp"], 6.75)
        self.assertEqual(tick["open_unrealized_gbp"], -3.25)

    def test_sum_open_unrealized_dedupes_deal_ids(self) -> None:
        tick = {
            "positions": [{"deal_id": "D1", "pnl_gbp": 12.5}],
            "markets": {
                "CS.D.CFPGOLD.CFP.IP": {
                    "positions": [{"deal_id": "D1", "pnl_gbp": 99.0}],
                }
            },
        }
        self.assertEqual(sum_open_unrealized_gbp(tick), 12.5)

    def test_fx_enrich_scales_from_ig_upl_not_config_point_value(self) -> None:
        """EUR/USD UPL is USD; £/pip must track IG contract via broker baseline."""
        quote = Quote(datetime(2026, 6, 12, 14, 0), 1.15738, 1.15742)
        base = [
            normalize_sync_position(
                {
                    "deal_id": "FX1",
                    "direction": "BUY",
                    "level": 1.15673,
                    "current": 1.15753,
                    "upl": 335.0,
                    "currency": "USD",
                    "size": 5.0,
                    "epic": "CS.D.EURUSD.CFD.IP",
                }
            )
        ]
        self.assertAlmostEqual(base[0]["pnl_gbp"], 335.0 * 0.78, places=2)
        out = enrich_positions_with_quote(
            base,
            quote,
            point_value_gbp=1.0,
            epic="CS.D.EURUSD.CFD.IP",
        )
        self.assertAlmostEqual(out[0]["current"], 1.15738)
        self.assertAlmostEqual(out[0]["pnl_pts"], 6.5, places=1)
        self.assertAlmostEqual(out[0]["pnl_gbp"], 335.0 * 0.78 * (6.5 / 8.0), places=0)
        self.assertAlmostEqual(out[0]["pnl_currency"], 335.0 * (6.5 / 8.0), places=0)

    def test_fx_instrument_spec_uses_usd_currency(self) -> None:
        from trading.open_position_view import instrument_pnl_spec

        spec = instrument_pnl_spec("CS.D.EURUSD.CFD.IP")
        self.assertEqual(spec["currency"], "USD")

    def test_apply_display_daily_pnl_adds_open_unrealized(self) -> None:
        tick = {
            "realized_daily_pnl_gbp": 10.0,
            "daily_pnl_gbp": 10.0,
            "positions": [{"deal_id": "D1", "pnl_gbp": -3.25}],
        }
        apply_display_daily_pnl(tick)
        self.assertEqual(tick["realized_daily_pnl_gbp"], 10.0)
        self.assertEqual(tick["open_unrealized_gbp"], -3.25)
        self.assertEqual(tick["daily_pnl_gbp"], 6.75)


if __name__ == "__main__":
    unittest.main()
