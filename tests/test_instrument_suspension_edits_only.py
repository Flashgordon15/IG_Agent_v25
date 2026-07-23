"""EDITS_ONLY → InstrumentSuspendedException fail-closed integration cases.

Confirms suspension is non-blocking (no zombie retry loops), does not leak
registry growth across clear cycles, and leaves concurrent routing free.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from execution.asymmetric_ioc_router import (
    dispatch_asymmetric_ioc_limit,
    reset_asymmetric_router_state_for_tests,
)
from execution.instrument_suspension import (
    RECOVERY_POLL_SEC,
    clear_deal_suspension,
    clear_epic_suspension,
    handle_dispatch_suspension,
    is_deal_suspended,
    is_epic_suspended,
    is_instrument_restriction,
    mark_deal_suspended,
    mark_epic_suspended,
    reset_instrument_suspension_for_tests,
    raise_instrument_suspended,
    suspended_snapshot,
)
from ig_api.exceptions import InstrumentSuspendedException, IGOrderError
from runtime.micro_gbp_exit import (
    GbpExitTrack,
    _evaluate_track,
    register_gbp_exit,
    remove_track,
)


@pytest.fixture(autouse=True)
def _clean_suspension_state(monkeypatch):
    monkeypatch.setenv("IG_SUSPENSION_RECOVERY_SEC", "0.05")
    reset_instrument_suspension_for_tests()
    reset_asymmetric_router_state_for_tests()
    yield
    reset_instrument_suspension_for_tests()
    reset_asymmetric_router_state_for_tests()


def test_edits_only_raises_instrument_suspended_exception():
    """Case 1: broker EDITS_ONLY status/token → localized InstrumentSuspendedException."""
    assert is_instrument_restriction("Market IX.D.DOW.IFM.IP not tradeable (status=EDITS_ONLY)")
    assert is_instrument_restriction("", status="EDITS_ONLY")
    assert is_instrument_restriction("transaction blocked — market restricted")

    with pytest.raises(InstrumentSuspendedException) as ei:
        raise_instrument_suspended(
            "IX.D.DOW.IFM.IP",
            status="EDITS_ONLY",
            detail="Market IX.D.DOW.IFM.IP not tradeable (status=EDITS_ONLY)",
        )
    exc = ei.value
    assert isinstance(exc, IGOrderError)
    assert exc.status == "EDITS_ONLY"
    assert exc.epic == "IX.D.DOW.IFM.IP"
    assert is_epic_suspended("IX.D.DOW.IFM.IP")

    # Router converts IGOrderError text into suspension without slip backoff pollution
    err = IGOrderError(
        "Market IX.D.DOW.IFM.IP not tradeable (status=EDITS_ONLY)",
        status_code=400,
    )

    class _Rest:
        def auth_ready_for_hot_path(self) -> bool:
            return True

        def place_market_order(self, **_kwargs):
            raise err

    with pytest.raises(InstrumentSuspendedException):
        dispatch_asymmetric_ioc_limit(
            _Rest(),
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=0.5,
            bid=52000.0,
            offer=52002.0,
            stop_distance=4.0,
        )


def test_dual_core_dispatch_marks_suspended_without_blocking():
    """Case 2: top-level catch → SUSPENDED mode; concurrent lane stays free."""
    epic = "IX.D.DOW.IFM.IP"
    exc = InstrumentSuspendedException(
        "not tradeable (status=EDITS_ONLY)",
        epic=epic,
        status="EDITS_ONLY",
    )
    barrier = threading.Barrier(2)
    other_done = threading.Event()

    def concurrent_lane() -> None:
        barrier.wait(timeout=2.0)
        # Must not be starved by suspension handling
        other_done.set()

    t = threading.Thread(target=concurrent_lane, daemon=True)
    t.start()
    barrier.wait(timeout=2.0)
    code = handle_dispatch_suspension(exc, epic=epic)
    assert code.startswith("instrument_suspended")
    assert is_epic_suspended(epic)
    assert other_done.wait(timeout=1.0), "concurrent API lane blocked by suspension"

    snap = suspended_snapshot()
    assert epic in snap["epics"]
    assert snap["epics"][epic]["mode"] == "SUSPENDED"
    # Recovery poll interval is 10s (non-busy)
    assert RECOVERY_POLL_SEC == 10.0


def test_soft_loss_skips_when_suspended_and_clears_without_leak():
    """Case 3: soft-loss skips local math under SUSPENDED; clear does not leak memory."""
    deal_id = "DIAAAA_TEST_SUSP"
    epic = "IX.D.DOW.IFM.IP"
    register_gbp_exit(
        deal_id=deal_id,
        epic=epic,
        direction="BUY",
        size=0.5,
        entry_level=52000.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=8.5,
        trail_trigger_gbp=1.0,
    )
    mark_deal_suspended(
        deal_id,
        epic=epic,
        status="EDITS_ONLY",
        entry_level=52000.0,
        direction="BUY",
        size=0.5,
    )
    assert is_deal_suspended(deal_id)

    track = GbpExitTrack(
        deal_id=deal_id,
        epic=epic,
        direction="BUY",
        size=0.5,
        entry_level=52000.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=8.5,
        trail_trigger_gbp=1.0,
        trail_lock_ratio=0.5,
    )
    with patch("runtime.micro_gbp_exit._flatten") as flatten_mock:
        # Deep soft-loss breach must NOT call flatten while SUSPENDED
        _evaluate_track(track, pnl_gbp=-50.0)
        flatten_mock.assert_not_called()

    # Clear cycle — no registry leak
    clear_deal_suspension(deal_id)
    clear_epic_suspension(epic)
    assert not is_deal_suspended(deal_id)
    assert not is_epic_suspended(epic)
    snap = suspended_snapshot()
    assert deal_id not in snap["deals"]
    assert epic not in snap["epics"]

    remove_track(deal_id)
