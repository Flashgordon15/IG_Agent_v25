"""Plausible mid bands — block poisoned EURUSD ~100 / index micro-channel."""

from __future__ import annotations

from system.quote_sanity import (
    filter_market_quotes,
    plausible_mid_for_epic,
    sanitize_quote_levels,
)


def test_eurusd_accepts_spot_rejects_dxy_band():
    assert plausible_mid_for_epic("CS.D.EURUSD.CFD.IP", 1.14128)
    assert not plausible_mid_for_epic("CS.D.EURUSD.CFD.IP", 100.09597)
    assert not plausible_mid_for_epic("CS.D.EURUSD.CFD.IP", 0.1)


def test_dow_rejects_micro_channel():
    assert plausible_mid_for_epic("IX.D.DOW.IFM.IP", 52003.4)
    assert not plausible_mid_for_epic("IX.D.DOW.IFM.IP", 100.09)


def test_filter_market_quotes_falls_back_to_prior():
    prior = {
        "CS.D.EURUSD.CFD.IP": {
            "epic": "CS.D.EURUSD.CFD.IP",
            "bid": 1.1410,
            "offer": 1.1412,
            "mid": 1.1411,
            "last_price": 1.1411,
            "source": "finnhub",
        }
    }
    poisoned = {
        "CS.D.EURUSD.CFD.IP": {
            "epic": "CS.D.EURUSD.CFD.IP",
            "bid": 100.08,
            "offer": 100.10,
            "mid": 100.09,
            "last_price": 100.09,
            "source": "yahoo",
        }
    }
    out = filter_market_quotes(poisoned, prior=prior)
    assert out["CS.D.EURUSD.CFD.IP"]["mid"] == 1.1411
    assert out["CS.D.EURUSD.CFD.IP"].get("stale_fallback") is True


def test_sanitize_quote_levels():
    ok = sanitize_quote_levels("CS.D.EURUSD.CFD.IP", bid=1.14, offer=1.141, mid=1.1405)
    assert ok is not None
    bad = sanitize_quote_levels("CS.D.EURUSD.CFD.IP", bid=100.0, offer=100.1, mid=100.05)
    assert bad is None
