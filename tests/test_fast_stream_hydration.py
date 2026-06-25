"""Fast-stream hydration fallback for Gate 5."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.fast_stream_hydration import (  # noqa: E402
    FAST_HYDRATION_WAIT_SEC,
    fast_stream_hydration_fallback,
    resolve_hydration_epics,
)
from system.market_data_hub import MarketDataHub, get_market_data_hub  # noqa: E402
from system.stream_ready import is_stream_ready, reset_stream_ready  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_hub_and_stream() -> None:
    import system.market_data_hub as hub_mod

    hub_mod._hub = MarketDataHub()
    reset_stream_ready()
    yield
    hub_mod._hub = None
    reset_stream_ready()


def test_resolve_hydration_epics_prefers_night_matrix() -> None:
    epics = resolve_hydration_epics(
        epics=["IX.D.FTSE.IFM.IP", "CS.D.EURUSD.CFD.IP", "ZZ.UNKNOWN"]
    )
    assert "IX.D.FTSE.IFM.IP" in epics
    assert "CS.D.EURUSD.CFD.IP" in epics
    assert "ZZ.UNKNOWN" not in epics


def test_fast_hydration_uses_existing_hub_tick_without_rest() -> None:
    hub = get_market_data_hub()
    hub.publish("CS.D.EURUSD.CFD.IP", 1.08, 1.0802, source="stream")
    rest = MagicMock()
    result = fast_stream_hydration_fallback(rest, epics=["CS.D.EURUSD.CFD.IP"], wait_sec=0.1)
    assert result["mode"] == "STREAM"
    rest.fetch_live_prices.assert_not_called()
    assert is_stream_ready()


def test_fast_hydration_rest_fallback_after_timeout() -> None:
    rest = MagicMock()
    rest.fetch_live_prices.return_value = (1.08, 1.0802)
    with patch(
        "system.fast_stream_hydration.FAST_HYDRATION_WAIT_SEC",
        0.05,
    ):
        result = fast_stream_hydration_fallback(
            rest,
            epics=["CS.D.EURUSD.CFD.IP"],
            wait_sec=0.05,
        )
    assert result["mode"] == "LIVE_FALLBACK"
    assert result["first_tick_epic"] == "CS.D.EURUSD.CFD.IP"
    snap = get_market_data_hub().get_snapshot("CS.D.EURUSD.CFD.IP")
    assert snap is not None and snap.bid > 0
    assert is_stream_ready()
    rest.fetch_live_prices.assert_called()


def test_fast_hydration_wait_constant_is_five_seconds() -> None:
    assert FAST_HYDRATION_WAIT_SEC == 5.0
