"""Tests for order_transmit_guard fail-closed behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from execution.order_transmit_guard import guard_order_transmit, log_transmit_block


def test_guard_rejects_missing_epic():
    allowed, size, reason = guard_order_transmit(
        epic="",
        direction="BUY",
        size=1.0,
        rest_client=MagicMock(),
    )
    assert allowed is False
    assert size == 0.0
    assert reason == "missing_epic"


def test_guard_rejects_none_rest_client():
    allowed, size, reason = guard_order_transmit(
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=1.0,
        rest_client=None,
    )
    assert allowed is False
    assert reason == "rest_client_unavailable"


def test_guard_fail_closed_on_tradeability_exception():
    rest = MagicMock()
    with patch(
        "execution.broker_tradeability.broker_new_deal_allowed",
        side_effect=RuntimeError("broker down"),
    ):
        allowed, size, reason = guard_order_transmit(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=1.0,
            rest_client=rest,
        )
    assert allowed is False
    assert size == 0.0
    assert reason == "market_status_unavailable:RuntimeError"


def test_guard_blocks_non_tradeable_market():
    rest = MagicMock()
    with patch(
        "execution.broker_tradeability.broker_new_deal_allowed",
        return_value=(False, "market_suspended"),
    ):
        allowed, size, reason = guard_order_transmit(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=1.0,
            rest_client=rest,
        )
    assert allowed is False
    assert reason == "market_suspended"


def test_guard_fail_closed_on_size_validation_exception():
    rest = MagicMock()
    with patch(
        "execution.broker_tradeability.broker_new_deal_allowed",
        return_value=(True, ""),
    ), patch(
        "execution.ig_size_validator.resolve_executable_lot_size",
        side_effect=ValueError("bad lot"),
    ), patch(
        "execution.broker_epic_resolver.resolve_order_epic_safe",
        return_value="IX.D.DOW.IFM.IP",
    ), patch(
        "execution.broker_epic_resolver.resolve_account_product",
        return_value="CFD",
    ):
        allowed, size, reason = guard_order_transmit(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=1.0,
            rest_client=rest,
        )
    assert allowed is False
    assert reason == "size_guard_ValueError"


def test_guard_allows_valid_order():
    rest = MagicMock()
    lot = MagicMock(ok=True, size=1.0, rejection_reason="")
    with patch(
        "execution.broker_tradeability.broker_new_deal_allowed",
        return_value=(True, ""),
    ), patch(
        "execution.ig_size_validator.resolve_executable_lot_size",
        return_value=lot,
    ), patch(
        "execution.broker_epic_resolver.resolve_order_epic_safe",
        return_value="IX.D.DOW.IFM.IP",
    ), patch(
        "execution.broker_epic_resolver.resolve_account_product",
        return_value="CFD",
    ), patch(
        "execution.ig_rest_traffic_governor.positions_otc_transmit_slot_available",
        return_value=True,
    ):
        allowed, size, reason = guard_order_transmit(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=0.5,
            rest_client=rest,
        )
    assert allowed is True
    assert size == 1.0
    assert reason == ""


def test_log_transmit_block_noop_on_empty_reason():
    log_transmit_block(epic="IX.D.DOW.IFM.IP", reason="", source="test")
