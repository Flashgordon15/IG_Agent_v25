"""Integration tests for trade lifecycle state machine."""

from __future__ import annotations

import pytest

from runtime.trade_lifecycle import (
    LifecycleState,
    begin_trade,
    reset_trade_lifecycle_for_tests,
    snapshot,
    transition,
)
from system.unified_runtime_state import reset_unified_runtime_state_for_tests


@pytest.fixture(autouse=True)
def _clean():
    reset_trade_lifecycle_for_tests()
    reset_unified_runtime_state_for_tests()
    yield
    reset_trade_lifecycle_for_tests()
    reset_unified_runtime_state_for_tests()


def test_lifecycle_happy_path():
    row = begin_trade(
        deal_id="TEST-1",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=1.0,
    )
    assert row["state"] == LifecycleState.ORDER_SUBMITTED.value
    transition("TEST-1", LifecycleState.ORDER_ACCEPTED, message="IG ok")
    transition("TEST-1", LifecycleState.TRAILING_STOP_ACTIVE, extra={"entry_level": 42000.0})
    transition("TEST-1", LifecycleState.ACTIVE)
    snap = snapshot()
    assert "TEST-1" in snap["active"]
    assert snap["active"]["TEST-1"]["state"] == LifecycleState.ACTIVE.value


def test_rejected_moves_to_history():
    begin_trade(deal_id="R1", epic="CS.D.EURUSD.CFD.IP", direction="SELL", size=0.5)
    transition("R1", LifecycleState.REJECTED, message="MINIMUM_ORDER_SIZE_ERROR")
    snap = snapshot()
    assert "R1" not in snap["active"]
    assert any(h["deal_id"] == "R1" for h in snap["history"])


def test_invalid_transition_ignored():
    begin_trade(deal_id="X1", epic="IX.D.DOW.IFM.IP", direction="BUY", size=1.0)
    transition("X1", LifecycleState.EXIT_FILLED)
    snap = snapshot()
    assert snap["active"]["X1"]["state"] == LifecycleState.ORDER_SUBMITTED.value


def test_full_state_chain():
    from runtime.trade_lifecycle import signal_detected

    signal_detected(epic="CS.D.CFPGOLD.CFP.IP", direction="BUY", deal_id="G1")
    transition("G1", LifecycleState.PRE_TRADE_VALIDATION)
    transition("G1", LifecycleState.ORDER_SUBMITTED)
    transition("G1", LifecycleState.ORDER_ACCEPTED)
    transition("G1", LifecycleState.ACTIVE)
    transition("G1", LifecycleState.DYNAMIC_LIMIT_ACTIVE)
    assert snapshot()["active"]["G1"]["state"] == LifecycleState.DYNAMIC_LIMIT_ACTIVE.value
