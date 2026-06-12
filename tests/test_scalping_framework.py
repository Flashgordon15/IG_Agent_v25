"""Scalping framework unit tests."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from data.models import Quote
from execution.scalping.atomic_protect import position_has_full_protection
from execution.scalping.breakeven_trail import (
    breakeven_trigger_points,
    trail_distance_from_atr,
)
from execution.scalping.dynamic_spread_filter import DynamicSpreadFilter
from execution.scalping.entry_halt import (
    clear_entry_halt_for_tests,
    halt_entries,
    is_entry_halted,
)
from execution.scalping.equity_circuit_breaker import EquityCircuitBreaker
from ig_api.mock_clients import MockIGRest, MockRESTConfig


@pytest.fixture(autouse=True)
def _reset_scalping_state():
    clear_entry_halt_for_tests()
    yield
    clear_entry_halt_for_tests()


def test_dynamic_spread_filter_blocks_toxic_spike():
    filt = DynamicSpreadFilter(periods=20, multiplier=1.5, min_samples=5)
    for _ in range(10):
        filt.record("EPIC", 2.0)
    ok, msg = filt.allows("EPIC", 2.0)
    assert ok is True
    ok, msg = filt.allows("EPIC", 5.0)
    assert ok is False
    assert "Spread filter" in msg


def test_breakeven_trigger_includes_spread_commission_buffer():
    q = Quote(time=datetime.now(timezone.utc), bid=100.0, offer=102.0)
    cfg = {
        "scalping_framework": {
            "commission_points_per_side": 0.5,
            "breakeven_buffer_points": 2.0,
        }
    }
    trigger = breakeven_trigger_points(q, cfg)
    assert trigger == pytest.approx(2.0 + 1.0 + 2.0)


def test_fx_breakeven_trigger_uses_pip_scale():
    q = Quote(time=datetime.now(timezone.utc), bid=1.15710, offer=1.15719)
    cfg = {
        "scalping_framework": {
            "commission_points_per_side": 0.5,
            "breakeven_buffer_points": 2.0,
        }
    }
    trigger = breakeven_trigger_points(q, cfg, epic="CS.D.EURUSD.CFD.IP")
    assert trigger == pytest.approx(3.9, abs=0.2)


def test_trail_distance_atr_half():
    cfg = {"scalping_framework": {"atr_trail_multiplier": 0.5}}
    assert trail_distance_from_atr(10.0, cfg) == pytest.approx(5.0)


def test_entry_halt_blocks_after_protection_failure():
    assert is_entry_halted() is False
    halt_entries("test protection fail")
    assert is_entry_halted() is True


def test_equity_circuit_breaker_trips_at_threshold():
    breaker = EquityCircuitBreaker(drawdown_pct=1.5)
    breaker.refresh_baseline(10_000.0)
    allowed, _ = breaker.check_equity(9_840.0)
    assert allowed is False


def test_mock_position_protection_status():
    client = MockIGRest()
    client.login()
    result = client.place_limit_entry_atomic(
        epic="IX.D.TEST",
        direction="BUY",
        size=1.0,
        level=100.0,
        stop_distance=10.0,
        limit_distance=30.0,
    )
    confirm = client.confirm_deal(result["dealReference"])
    deal_id = confirm["deal_id"]
    assert position_has_full_protection(client, deal_id) is True
