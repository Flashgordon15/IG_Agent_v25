"""Tests for Yahoo reference quote poller."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from feeder.pricing_transport import reference_transport, reference_transport_is_yahoo
from feeder.yahoo_quote_poller import (
    YahooQuotePoller,
    fetch_yahoo_quote,
    reset_yahoo_quote_poller_for_tests,
    yahoo_quote_from_mid,
)


class PricingTransportTests(unittest.TestCase):
    def test_reference_transport_defaults_ig(self) -> None:
        self.assertEqual(reference_transport({}), "ig")
        self.assertFalse(reference_transport_is_yahoo({}))

    def test_reference_transport_config_yahoo(self) -> None:
        cfg = {"pricing": {"reference_transport": "yahoo"}}
        self.assertEqual(reference_transport(cfg), "yahoo")
        self.assertTrue(reference_transport_is_yahoo(cfg))


class YahooQuotePollerTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_yahoo_quote_poller_for_tests()

    def test_yahoo_quote_from_mid_fx(self) -> None:
        sample = yahoo_quote_from_mid("CS.D.EURUSD.CFD.IP", 1.0850, "EURUSD=X")
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(sample.mid, 1.0850)
        self.assertLess(sample.bid, sample.offer)

    def test_yahoo_quote_from_mid_rejects_dxy_scale_fx(self) -> None:
        sample = yahoo_quote_from_mid("CS.D.EURUSD.CFD.IP", 100.09, "EURUSD=X")
        self.assertIsNone(sample)

    @patch("feeder.yahoo_quote_poller.fetch_yahoo_mid", return_value=42000.0)
    def test_fetch_yahoo_quote(self, _mid: MagicMock) -> None:
        sample = fetch_yahoo_quote("IX.D.DOW.IFM.IP")
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.epic, "IX.D.DOW.IFM.IP")
        self.assertGreater(sample.offer, sample.bid)

    @patch("feeder.yahoo_quote_poller.fetch_yahoo_quote")
    def test_poller_publishes_to_hub(self, fetch_mock: MagicMock) -> None:
        from system.market_data_hub import get_market_data_hub

        from feeder.yahoo_quote_poller import YahooQuoteSample

        fetch_mock.return_value = YahooQuoteSample(
            epic="CS.D.CFPGOLD.CFP.IP",
            symbol="GC=F",
            mid=2400.0,
            bid=2399.5,
            offer=2400.5,
        )
        poller = YahooQuotePoller(poll_sec=1.0)
        poller.start(["CS.D.CFPGOLD.CFP.IP"])
        poller.poll_epic("CS.D.CFPGOLD.CFP.IP")
        snap = get_market_data_hub().get_snapshot("CS.D.CFPGOLD.CFP.IP")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.source, "yahoo")
        self.assertGreater(snap.offer, snap.bid)
        poller.stop()


if __name__ == "__main__":
    unittest.main()
