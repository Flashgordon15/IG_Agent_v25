"""Tests for IG size validator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from execution.ig_size_validator import (
    SizeValidationResult,
    classify_size_rejection,
    validate_order_size,
)


class _FakeRest:
    def fetch_market_constraints(self, epic: str) -> dict:
        return {
            "min_deal_size": 0.5,
            "max_deal_size": 100.0,
            "deal_increment": 0.1,
        }


def test_validate_bumps_to_min_deal(monkeypatch):
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    rest = _FakeRest()
    result = validate_order_size(
        "IX.D.DOW.IFM.IP",
        0.1,
        "BUY",
        None,
        rest,
        broker_epic="IX.D.DOW.IFM.IP",
    )
    assert isinstance(result, SizeValidationResult)
    assert result.ok is True
    assert result.adjusted_size >= 0.5
    assert result.ig_min_deal >= 0.5


def test_canary_cap_applied():
    rest = _FakeRest()
    result = validate_order_size(
        "IX.D.DOW.IFM.IP",
        999.0,
        "BUY",
        None,
        rest,
        broker_epic="IX.D.DOW.IFM.IP",
    )
    assert result.adjusted_size <= 100.0


def test_classify_size_rejection():
    assert classify_size_rejection("MINIMUM_ORDER_SIZE_ERROR") is True
    assert classify_size_rejection("MARGIN") is False


def test_no_rest_client_applies_hard_floor_gold(monkeypatch):
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    result = validate_order_size("CS.D.CFPGOLD.CFP.IP", 1.0, "BUY", None, None)
    assert result.ok is True
    assert result.adjusted_size >= 10.0
    assert result.ig_min_deal >= 10.0


def test_no_rest_client_still_ok_with_positive_size(monkeypatch):
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    result = validate_order_size("IX.D.DOW.IFM.IP", 1.0, "BUY", None, None)
    assert result.ok is True
    assert result.adjusted_size >= 0.5


def test_resolve_executable_fractional_spreadbet_demo(monkeypatch):
    from execution.ig_size_validator import resolve_executable_lot_size

    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    rest = _FakeRest()
    lot = resolve_executable_lot_size(
        "IX.D.DOW.IFM.IP",
        0.5,
        "BUY",
        None,
        rest,
        broker_epic="IX.D.DOW.IFM.IP",
    )
    assert lot.ok is True
    assert lot.size >= 0.5


def test_resolve_executable_integer_cfd_path(monkeypatch):
    from execution.ig_size_validator import resolve_executable_lot_size

    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: False,
    )
    lot = resolve_executable_lot_size("IX.D.DOW.IFM.IP", 0.5, "BUY", None, None)
    assert lot.ok is True
    assert lot.size == 1.0
    lot0 = resolve_executable_lot_size("IX.D.DOW.IFM.IP", 0.0, "BUY", None, None)
    assert lot0.ok is True
    assert lot0.size == 1.0
    lot2 = resolve_executable_lot_size("IX.D.DOW.IFM.IP", 1.2, "BUY", None, None)
    assert lot2.ok is True
    assert lot2.size == 1.0
