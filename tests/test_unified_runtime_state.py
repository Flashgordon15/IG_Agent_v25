"""Tests for unified runtime state singleton."""

from __future__ import annotations

import pytest

from system.unified_runtime_state import (
    emit_event,
    get_rejections,
    init_unified_runtime_state,
    record_rejection,
    reset_unified_runtime_state_for_tests,
    snapshot,
    update_execution,
    update_lifecycle_trade,
    update_routing,
    update_sizing,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_unified_runtime_state_for_tests()
    yield
    reset_unified_runtime_state_for_tests()


def test_init_and_snapshot():
    init_unified_runtime_state()
    snap = snapshot()
    assert snap["ok"] is True
    assert "boot" in snap
    assert "startup_diagnostics" in snap


def test_emit_event_ring_buffer():
    init_unified_runtime_state()
    emit_event("test", {"x": 1})
    snap = snapshot()
    assert any(e["type"] == "test" for e in snap["events"])


def test_record_rejection():
    init_unified_runtime_state()
    record_rejection(
        epic="IX.D.DOW.IFM.IP",
        reason="MINIMUM_ORDER_SIZE_ERROR",
        classification="SIZE",
        self_correction_attempted=True,
    )
    rej = get_rejections(limit=5)
    assert len(rej) == 1
    assert rej[0]["classification"] == "SIZE"
    assert rej[0]["self_correction_attempted"] is True


def test_lifecycle_trade_updates():
    init_unified_runtime_state()
    update_lifecycle_trade("D123", "ACTIVE", epic="IX.D.DOW.IFM.IP", direction="BUY")
    snap = snapshot()
    assert snap["lifecycle"]["active_trades"]["D123"]["state"] == "ACTIVE"
    update_lifecycle_trade("D123", "CLOSED")
    assert "D123" not in snapshot()["lifecycle"]["active_trades"]


def test_routing_and_execution_hooks():
    init_unified_runtime_state()
    update_routing(armed_count=5, rotation_sweep_count=10, rotation_active=True)
    update_execution(loop_active=True, execution_loop_ready=True)
    update_sizing(rules_loaded=True, epic="CS.D.EURUSD.CFD.IP", validation={"ok": True})
    diag = snapshot()["startup_diagnostics"]
    assert diag["rotation_logic_active"] is True
    assert diag["execution_loop_ready"] is True
    assert diag["size_rules_loaded"] is True
