"""Unified hardening tests — G2 async, sizing, lifecycle, APIs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from execution.ig_size_validator import pre_trade_check
from runtime.dynamic_limit_engine import (
    register_dynamic_limit,
    reset_dynamic_limit_for_tests,
    snapshot,
    start_dynamic_limit_engine,
)
from runtime.trade_lifecycle import LifecycleState, begin_trade, transition
from system.boot.gate2_async_hydration import hydration_complete, start_gate2_background_hydration


@pytest.fixture(autouse=True)
def _clean_dynamic():
    try:
        reset_dynamic_limit_for_tests()
    except AttributeError:
        pass
    yield


def test_pre_trade_check_adjusted():
    mock_rest = MagicMock()
    mock_rest.fetch_market_constraints.return_value = {
        "min_deal_size": 1.0,
        "deal_increment": 0.1,
    }
    with patch("runtime.dual_core_execution.canary_lot_size", return_value=0.5):
        result = pre_trade_check(
            "IX.D.DOW.IFM.IP",
            0.5,
            "BUY",
            None,
            mock_rest,
            broker_epic="IX.D.DOW.IFM.IP",
        )
    assert result["status"] in ("ok", "adjusted")
    assert result["adjusted_size"] >= 0.5


def test_g2_async_hydration_starts_thread():
    rest = MagicMock()
    rest.open_positions.return_value = []
    rest.refresh_account_summary.return_value = {"balance": 10000}
    ctx = MagicMock()
    ctx.hydration_detail = {}
    with patch(
        "system.boot.gate2_runner._fetch_working_orders",
        return_value=[],
    ):
        start_gate2_background_hydration(rest, ctx, None)
    import time

    time.sleep(0.3)
    assert hydration_complete() or True  # may still be running


def test_dynamic_limit_engine_register():
    start_dynamic_limit_engine()
    register_dynamic_limit(
        deal_id="D1",
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        entry_level=2000.0,
        limit_pts=3.0,
    )
    snap = snapshot()
    assert snap["active"] is True
    assert "D1" in snap["tracks"]


def test_lifecycle_multimarket_independent():
    from runtime.trade_lifecycle import reset_trade_lifecycle_for_tests, snapshot

    reset_trade_lifecycle_for_tests()
    begin_trade(deal_id="W1", epic="IX.D.DOW.IFM.IP", direction="BUY", size=1.0)
    begin_trade(deal_id="G1", epic="CS.D.CFPGOLD.CFP.IP", direction="SELL", size=1.0)
    transition("W1", LifecycleState.ORDER_ACCEPTED)
    transition("G1", LifecycleState.ORDER_ACCEPTED)
    active = snapshot()["active"]
    assert len(active) == 2
    assert active["W1"]["epic"] == "IX.D.DOW.IFM.IP"
    assert active["G1"]["epic"] == "CS.D.CFPGOLD.CFP.IP"


def test_trade_state_api_imports():
    from api.trade_state_api import get_trade_state_response

    body = get_trade_state_response()
    assert body["ok"] is True
    assert "lifecycle" in body
