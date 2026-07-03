"""Market integrity — closed exchange and stale quote enforcement."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from data.models import Quote


class MarketIntegrityTests(unittest.TestCase):
    def test_closed_market_blocks_publish(self) -> None:
        import system.market_integrity as mi

        epic = "IX.D.NIKKEI.IFM.IP"
        with patch.object(mi, "epic_market_open", return_value=False):
            self.assertFalse(mi.epic_market_open(epic))
            q = Quote(datetime.now(timezone.utc), 100.0, 100.5)
            verdict = mi.check_quote_integrity(epic, q)
            self.assertFalse(verdict.allowed)
            self.assertEqual(verdict.market_state, "CLOSED")
            self.assertEqual(verdict.stream_status, "CLOSED")

    def test_stale_quote_blocks_within_500ms_budget(self) -> None:
        from system.market_integrity import check_quote_integrity

        epic = "CS.D.CFPGOLD.CFP.IP"
        old = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
        q = Quote(old, 2400.0, 2400.5)
        with patch("system.market_integrity.epic_market_open", return_value=True):
            verdict = check_quote_integrity(epic, q)
            self.assertFalse(verdict.allowed)
            self.assertIn("STALE", verdict.reason.upper())

    def test_merge_hub_does_not_promote_offline_to_open(self) -> None:
        from api.snapshot_store import _merge_hub_quote_into_tick

        tick = {
            "markets": {
                "CS.D.CFPGOLD.CFP.IP": {
                    "market_state": "CLOSED",
                    "bid": 0,
                    "offer": 0,
                }
            }
        }
        with patch("system.market_integrity.epic_market_open", return_value=True):
            _merge_hub_quote_into_tick(
                tick, "CS.D.CFPGOLD.CFP.IP", 2400.0, 2400.5, tick_age_s=0.1
            )
        self.assertEqual(
            tick["markets"]["CS.D.CFPGOLD.CFP.IP"]["market_state"], "CLOSED"
        )

    def test_hub_publish_blocked_when_closed(self) -> None:
        from system.market_data_hub import MarketDataHub

        hub = MarketDataHub()
        with patch("system.market_integrity.should_publish_live_quote", return_value=False):
            result = hub.publish("IX.D.DOW.IFM.IP", 100.0, 100.5, source="test")
        self.assertIsNone(result)
        self.assertIsNone(hub.get_snapshot("IX.D.DOW.IFM.IP"))

    def test_hub_publish_blocked_preserves_existing_reference_quote(self) -> None:
        from system.market_data_hub import MarketDataHub

        hub = MarketDataHub()
        hub.publish("CS.D.CFPGOLD.CFP.IP", 2400.0, 2400.5, source="yahoo")
        with patch("system.market_integrity.should_publish_live_quote", return_value=False):
            result = hub.publish("CS.D.CFPGOLD.CFP.IP", 2300.0, 2300.5, source="rest")
        self.assertIsNotNone(result)
        self.assertEqual(result.bid, 2400.0)

    def test_night_matrix_yahoo_allowed_when_exchange_closed(self) -> None:
        from system.market_integrity import should_publish_live_quote

        epic = "IX.D.FTSE.IFM.IP"
        with patch("system.market_integrity.epic_market_open", return_value=False):
            with patch(
                "system.market_integrity.night_matrix_reference_always_on",
                return_value=True,
            ):
                self.assertTrue(should_publish_live_quote(epic, source="yahoo"))


if __name__ == "__main__":
    unittest.main()
