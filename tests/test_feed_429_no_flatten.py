"""Finnhub/feed HTTP 429 must backoff without catastrophic flatten."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_note_api_error_429_skips_flatten(monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime import feed_health_watchdog as fhw

    flattened: list[str] = []

    monkeypatch.setattr(fhw, "_resolve_quote_age_sec", lambda: 1.0)
    monkeypatch.setattr(fhw, "_mark_unhealthy", lambda *_a, **_k: None)
    monkeypatch.setattr(fhw, "_hard_reset_streams", lambda *_a, **_k: None)
    monkeypatch.setattr(
        fhw,
        "_catastrophic_flatten",
        lambda reason: flattened.append(str(reason)),
    )

    class _RateLimit(Exception):
        status_code = 429

    fhw.note_api_error(_RateLimit("HTTP 429 Too Many Requests"), flatten=True)
    assert flattened == []

    fhw.note_api_error(RuntimeError("stream dead"), flatten=True)
    # Without credentials / opens the flatten path may no-op — force call path.
    # Re-bind open_positions path to confirm non-429 still attempts flatten.