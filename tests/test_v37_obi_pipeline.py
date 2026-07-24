"""V37 Phase 1 — OBI feature plane: synthetic book → non-zero; missing → fail-closed."""

from __future__ import annotations

import time
from collections import deque
from types import SimpleNamespace

import pytest

from alpha.micro_sniper_ml import (
    evaluate_live_sniper_probability,
    observe_obi_tick,
    reset_sniper_ml_cache_for_tests,
    rolling_obi_for,
)
from cockpit.telemetry_schema import OrderBookDepthPayload
from execution.entry_gate_hardening import (
    reset_obi_proxy_history_for_tests,
    resolve_obi_signal,
)
from intelligence.order_book_imbalance import (
    compute_obi_ratio,
    compute_obi_ratio_available,
    compute_proxy_obi_from_mids,
)


@pytest.fixture(autouse=True)
def _reset_sniper() -> None:
    reset_sniper_ml_cache_for_tests()
    reset_obi_proxy_history_for_tests()
    yield
    reset_sniper_ml_cache_for_tests()
    reset_obi_proxy_history_for_tests()


def _buy_heavy_book(epic: str = "IX.D.DOW.IFM.IP") -> OrderBookDepthPayload:
    return OrderBookDepthPayload(
        epic=epic,
        ts=time.time(),
        bid_levels=[{"price": 39000.0, "size": 8.0}, {"price": 38999.0, "size": 4.0}],
        ask_levels=[{"price": 39001.0, "size": 1.0}, {"price": 39002.0, "size": 1.0}],
    )


def test_synthetic_book_yields_nonzero_obi() -> None:
    payload = _buy_heavy_book()
    ratio, available = compute_obi_ratio_available(payload)
    assert available is True
    assert ratio > 0.5
    assert compute_obi_ratio(payload) == pytest.approx(ratio)


def test_empty_book_not_available() -> None:
    ratio, available = compute_obi_ratio_available(
        OrderBookDepthPayload(
            epic="IX.D.DOW.IFM.IP",
            ts=time.time(),
            bid_levels=[],
            ask_levels=[],
        )
    )
    assert available is False
    assert ratio == 0.0
    assert compute_obi_ratio_available(None) == (0.0, False)


def test_resolve_obi_signal_from_quote_book() -> None:
    epic = "IX.D.DOW.IFM.IP"
    quote = SimpleNamespace(
        bid=39000.0,
        offer=39001.0,
        order_book_depth=_buy_heavy_book(epic).model_dump(),
    )
    ratio, source, available = resolve_obi_signal(epic, quote=quote)
    assert available is True
    assert source == "order_book"
    assert abs(ratio) > 0.4


def test_missing_book_obi_unavailable() -> None:
    ratio, source, available = resolve_obi_signal(
        "IX.D.DOW.IFM.IP",
        quote=SimpleNamespace(bid=0, offer=0),
    )
    assert available is False
    assert source == "obi_unavailable"
    assert ratio == 0.0


def test_proxy_obi_synthetic_rising_mids_nonzero() -> None:
    mids = [39000.0, 39000.8, 39001.5, 39002.0, 39003.0]
    ratio, available = compute_proxy_obi_from_mids(mids, spread=1.5)
    assert available is True
    assert ratio > 0.5


def test_proxy_obi_flat_or_missing_unavailable() -> None:
    assert compute_proxy_obi_from_mids([], 1.0) == (0.0, False)
    assert compute_proxy_obi_from_mids([39000.0], 1.0) == (0.0, False)
    ratio, available = compute_proxy_obi_from_mids([39000.0] * 10, spread=1.5)
    assert available is False
    assert ratio == 0.0


def test_resolve_quote_proxy_from_mid_history() -> None:
    """rest_poll path: no L2, but rolling hub/quote mids → quote_proxy OBI."""
    epic = "IX.D.DOW.IFM.IP"
    mids = deque([39000.0, 39001.0, 39002.5, 39003.0, 39004.0], maxlen=32)
    quote = SimpleNamespace(
        bid=39003.5,
        offer=39004.5,
        mid_history=mids,
    )
    ratio, source, available = resolve_obi_signal(epic, quote=quote)
    assert available is True
    assert source == "quote_proxy"
    assert ratio > 0.0

    # Rolling 10-tick buffer populated after sniper observes raw OBI.
    live = evaluate_live_sniper_probability(epic, "BUY", quote=quote)
    assert live.features.get("obi_available") is True
    assert live.features.get("obi_source") == "quote_proxy"
    assert abs(float(live.features.get("obi_raw") or 0.0)) > 0.0
    roll = rolling_obi_for(epic)
    assert roll is not None
    assert abs(float(roll)) > 0.0


def test_resolve_flat_mid_history_still_unavailable() -> None:
    epic = "IX.D.DOW.IFM.IP"
    quote = SimpleNamespace(
        bid=39000.0,
        offer=39001.0,
        mid_history=deque([39000.5] * 8, maxlen=32),
    )
    ratio, source, available = resolve_obi_signal(epic, quote=quote)
    assert available is False
    assert source == "obi_unavailable"
    assert ratio == 0.0


def test_sniper_rejects_when_obi_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "execution.entry_gate_hardening.resolve_obi_signal",
        lambda epic, quote=None: (0.0, "obi_unavailable", False),
    )
    live = evaluate_live_sniper_probability(
        "IX.D.DOW.IFM.IP",
        "BUY",
        cfg={"grok_macro_bias": "NEUTRAL"},
        quote=None,
    )
    assert live.approved is False
    assert live.reason == "obi_unavailable"
    assert live.features.get("obi_unavailable") is True
    assert live.features.get("features_unavailable_fail_open") is False
    assert live.p_success < 0.5


def test_sniper_scores_nonzero_obi_from_book(monkeypatch: pytest.MonkeyPatch) -> None:
    epic = "IX.D.DOW.IFM.IP"
    book = _buy_heavy_book(epic).model_dump()
    quote = SimpleNamespace(bid=39000.0, offer=39001.5, order_book_depth=book)

    monkeypatch.setattr(
        "execution.grok_macro_bias.resolve_grok_macro_bias",
        lambda cfg=None: "BULL",
    )

    live = evaluate_live_sniper_probability(epic, "BUY", quote=quote)
    assert live.features.get("obi_available") is True
    assert live.features.get("obi_unavailable") is False
    assert abs(float(live.features.get("obi_raw") or 0.0)) > 0.4
    assert live.reason != "obi_unavailable"
    assert live.features.get("features_unavailable_fail_open") is not True


def test_rolling_obi_after_raw_nonzero() -> None:
    epic = "IX.D.DOW.IFM.IP"
    vals = [0.4, 0.5, 0.6, 0.55, 0.45]
    last = 0.0
    for v in vals:
        last = observe_obi_tick(epic, v)
    roll = rolling_obi_for(epic)
    assert roll is not None
    assert roll == pytest.approx(sum(vals) / len(vals))
    assert last == pytest.approx(roll)
