"""Data feed orchestrator and hub publish guard tests."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_orchestrator():
    from system.feeds.data_feed_orchestrator import reset_data_feed_orchestrator_for_tests
    from system.packet_validator import reset_packet_validator_for_tests

    reset_data_feed_orchestrator_for_tests()
    reset_packet_validator_for_tests()
    yield
    reset_data_feed_orchestrator_for_tests()
    reset_packet_validator_for_tests()


def test_hub_blocks_ig_overwrite_fresh_yahoo(monkeypatch):
    from system.market_data_hub import MarketDataHub

    monkeypatch.setenv("IG_PRICING_REFERENCE", "yahoo")
    monkeypatch.setenv("IG_TEST_HARNESS", "1")
    hub = MarketDataHub()
    first = hub.publish("IX.D.DOW.IFM.IP", 42000.0, 42002.0, source="yahoo_poll")
    assert first is not None
    blocked = hub.publish("IX.D.DOW.IFM.IP", 41000.0, 41002.0, source="rest")
    assert blocked is not None
    assert blocked.bid == 42000.0
    assert blocked.source == "yahoo_poll"


def test_fetch_if_stale_skips_ig_when_yahoo_primary(monkeypatch):
    from system.market_data_hub import MarketDataHub

    monkeypatch.setenv("IG_PRICING_REFERENCE", "yahoo")
    hub = MarketDataHub()

    class _FakeRest:
        def fetch_live_prices(self, epic: str):
            raise AssertionError("IG REST must not be called on signal path")

    hub.attach_rest(_FakeRest())
    hub.publish("CS.D.CFPGOLD.CFP.IP", 2400.0, 2400.5, source="yahoo_poll")
    snap = hub.fetch_if_stale("CS.D.CFPGOLD.CFP.IP", min_interval=0.0, max_age=999.0)
    assert snap is not None
    assert snap.source == "yahoo_poll"


def test_get_quote_alias_matches_get_snapshot():
    from system.market_data_hub import MarketDataHub

    hub = MarketDataHub()
    hub.publish("CS.D.EURUSD.CFD.IP", 1.08, 1.0802, source="test")
    snap = hub.get_snapshot("CS.D.EURUSD.CFD.IP")
    quote = hub.get_quote("CS.D.EURUSD.CFD.IP")
    assert quote is snap
    assert quote is not None and quote.bid == 1.08


def test_orchestrator_state_shape(monkeypatch):
    from system.feeds.data_feed_orchestrator import get_data_feed_state

    body = get_data_feed_state()
    assert "health" in body
    assert "primary_feed" in body
    assert "feeds" in body
    assert "yahoo" in body["feeds"]


def test_hub_blocks_ig_initial_publish_yahoo_primary(monkeypatch):
    from system.market_data_hub import MarketDataHub

    monkeypatch.setenv("IG_PRICING_REFERENCE", "yahoo")
    monkeypatch.setenv("IG_TEST_HARNESS", "1")
    hub = MarketDataHub()
    blocked = hub.publish("IX.D.DOW.IFM.IP", 42000.0, 42002.0, source="rest")
    assert blocked is None


def test_signal_feed_rejects_ig_on_signal_path(monkeypatch):
    from system.feeds.data_feed_orchestrator import signal_feed_health_ok
    from system.market_data_hub import get_market_data_hub

    monkeypatch.setenv("IG_PRICING_REFERENCE", "yahoo")
    hub = get_market_data_hub()
    hub.publish("CS.D.CFPGOLD.CFP.IP", 2400.0, 2400.5, source="rest")
    assert signal_feed_health_ok() is False


def test_fast_stream_hydration_blocks_ig_rest_in_yahoo_mode(monkeypatch):
    from system.fast_stream_hydration import _inject_rest_quotes

    monkeypatch.setenv("IG_PRICING_REFERENCE", "yahoo")

    class _FakeRest:
        def fetch_live_prices(self, epic: str):
            raise AssertionError("IG REST must not run on Yahoo signal path")

    hydrated, first = _inject_rest_quotes(_FakeRest(), ["CS.D.CFPGOLD.CFP.IP"])
    assert hydrated == []
    assert first is None


def test_ingest_fresh_quote_publishes_to_hub(monkeypatch):
    from runtime.dual_core_execution import _ingest_fresh_quote
    from system.market_data_hub import MarketDataHub

    hub = MarketDataHub()
    monkeypatch.setattr("runtime.dual_core_execution.get_market_data_hub", lambda: hub)
    _ingest_fresh_quote(
        "CS.D.CFPGOLD.CFP.IP",
        2400.0,
        2400.5,
        source="yahoo",
    )
    snap = hub.get_snapshot("CS.D.CFPGOLD.CFP.IP")
    assert snap is not None
    assert snap.bid == 2400.0
    assert snap.age_seconds() <= 5.0
