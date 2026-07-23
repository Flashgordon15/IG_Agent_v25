"""v33 maintenance detachment — CORE_DETACHED suppresses broker dispatch only."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from execution.asymmetric_ioc_router import dispatch_asymmetric_ioc_limit
from execution.atomic_gateway import dispatch_atomic_market_order
from execution.maintenance_detachment import is_core_detached, suppress_order_dispatch
from kernel.shm_facade import publish_tick, read_latest_tick, reset_shm_facade_for_tests
from kernel.ring_buffer import PositionRingBuffer, reset_ring_buffer_for_tests


@pytest.fixture(autouse=True)
def _env_cleanup() -> None:
    old = os.environ.get("CORE_DETACHED")
    yield
    if old is None:
        os.environ.pop("CORE_DETACHED", None)
    else:
        os.environ["CORE_DETACHED"] = old


def test_is_core_detached_env_gate() -> None:
    os.environ["CORE_DETACHED"] = "FALSE"
    assert is_core_detached() is False
    os.environ["CORE_DETACHED"] = "TRUE"
    assert is_core_detached() is True


def test_suppress_order_dispatch_returns_mock_confirm() -> None:
    os.environ["CORE_DETACHED"] = "TRUE"
    pkt = suppress_order_dispatch(
        source="unit_test",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        action="entry",
    )
    assert pkt["core_detached"] is True
    assert str(pkt["dealReference"]).startswith("MOCK-DETACHED-")
    confirm = pkt.get("confirm") or {}
    assert confirm.get("accepted") is True
    assert confirm.get("deal_reference") == pkt["dealReference"]


def test_order_dispatch_layers_skip_ig_rest() -> None:
    os.environ["CORE_DETACHED"] = "TRUE"
    rest = MagicMock()
    rest.place_otc_market_payload = MagicMock()
    rest.place_market_order = MagicMock()
    rest.request = MagicMock()

    asym = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert asym.get("core_detached") is True
    rest.place_otc_market_payload.assert_not_called()
    rest.place_market_order.assert_not_called()

    atomic = dispatch_atomic_market_order(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=1.0,
        stop_distance=4.0,
    )
    assert atomic.get("core_detached") is True
    rest.place_market_order.assert_not_called()
    rest.request.assert_not_called()


def test_passive_shm_tick_surveillance_when_detached() -> None:
    os.environ["CORE_DETACHED"] = "TRUE"
    reset_shm_facade_for_tests()
    reset_ring_buffer_for_tests()
    name = f"ig_agent_v33_detach_{os.getpid()}"
    os.environ["IG_SHM_RING_NAME"] = name
    os.environ["IG_SHM_RING_CREATE"] = "1"
    try:
        PositionRingBuffer.create(name=name)
        epic = "IX.D.DOW.IFM.IP"
        seq = publish_tick(epic=epic, bid=45000.0, offer=45002.0)
        assert seq is not None
        row = read_latest_tick(epic)
        assert row is not None
        assert float(row.get("bid") or 0) == pytest.approx(45000.0)
        assert float(row.get("offer") or 0) == pytest.approx(45002.0)
    finally:
        reset_shm_facade_for_tests()
        reset_ring_buffer_for_tests()
        os.environ.pop("IG_SHM_RING_NAME", None)
        os.environ.pop("IG_SHM_RING_CREATE", None)
