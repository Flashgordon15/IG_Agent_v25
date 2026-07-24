"""V37 Phase 1 — OBI feature plane: synthetic book → non-zero; missing → fail-closed."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from alpha.micro_sniper_ml import (
    evaluate_live_sniper_probability,
    observe_obi_tick,
    reset_sniper_ml_cache_for_tests,
    rolling_obi_for,
)
from cockpit.telemetry_schema import OrderBookDepthPayload
from execution.entry_gate_hardening import resolve_obi_signal
from intelligence.order_book_imbalance import (
    compute_obi_ratio,
    compute_obi_ratio_available,
)


@pytest.fixture(autouse=True)
def _reset_sniper() -> None:
    reset_sniper_ml_cache_for_tests()
    yield
    reset_sniper_ml_cache_for_tests()


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
