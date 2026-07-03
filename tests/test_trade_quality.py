"""Tests for trade_quality snapshot."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from analytics.trade_quality import (
    get_trade_quality_snapshot,
    reset_trade_quality_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_trade_quality_for_tests()
    yield
    reset_trade_quality_for_tests()


def test_refresh_computes_acceptance_rate():
    with patch("system.unified_runtime_state.get_rejections", return_value=[{"reason": "size"}]):
        with patch(
            "runtime.trade_lifecycle.snapshot",
            return_value={"active": {"d1": {"state": "ACTIVE"}}, "history": []},
        ):
            with patch("analytics.trade_quality._slippage_from_triage", return_value={"samples": 0}):
                with patch("analytics.trade_quality._lifecycle_event_counts", return_value=(2, 1)):
                    with patch("analytics.trade_quality._risk_vs_pnl", return_value={"daily_pnl_gbp": 10.0}):
                        with patch("runtime.broker_reject_guard.broker_reject_guard_status", return_value={}):
                            from analytics import trade_quality

                            trade_quality._refresh_snapshot()
    snap = get_trade_quality_snapshot()
    assert snap["ok"] is True
    assert snap["orders_rejected"] == 1
    assert snap["orders_accepted"] == 1
    assert snap["acceptance_rate"] == 0.5
    assert snap["trailing_events"] == 2


def test_snapshot_copy_isolated():
    from analytics import trade_quality

    trade_quality._snapshot["orders_accepted"] = 42
    snap = get_trade_quality_snapshot()
    snap["orders_accepted"] = 0
    assert get_trade_quality_snapshot()["orders_accepted"] == 42
