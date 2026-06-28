"""Tests for broker reject circuit breaker."""

from __future__ import annotations

from runtime.broker_reject_guard import (
    broker_reject_dispatch_blocked,
    configure_broker_reject_guard,
    record_broker_confirm_rejection,
    record_broker_confirm_success,
    reset_broker_reject_guard_for_tests,
)


def setup_function() -> None:
    reset_broker_reject_guard_for_tests()
    configure_broker_reject_guard(trip_threshold=3, latch_sec=60.0)


def teardown_function() -> None:
    reset_broker_reject_guard_for_tests()


def test_reject_guard_trips_after_threshold():
    for _ in range(2):
        record_broker_confirm_rejection(
            reason="INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY",
            epic="CS.D.GBPUSD.CFD.IP",
            broker_epic="CS.D.GBPUSD.CFD.IP",
        )
        blocked, _ = broker_reject_dispatch_blocked()
        assert blocked is False
    record_broker_confirm_rejection(
        reason="INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY",
        epic="CS.D.GBPUSD.CFD.IP",
        broker_epic="CS.D.GBPUSD.CFD.IP",
    )
    blocked, reason = broker_reject_dispatch_blocked()
    assert blocked is True
    assert "broker_reject_latched" in reason


def test_success_clears_reject_guard():
    for _ in range(3):
        record_broker_confirm_rejection(reason="INSTRUMENT_NOT_TRADEABLE")
    record_broker_confirm_success()
    blocked, _ = broker_reject_dispatch_blocked()
    assert blocked is False
