"""Tests for IG budget monitor, micro risk profile, execution guard."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic
from system.ig_budget_monitor import ig_budget_snapshot, reset_ig_budget_monitor_for_tests


@pytest.fixture(autouse=True)
def _clean():
    reset_ig_budget_monitor_for_tests()
    yield
    reset_ig_budget_monitor_for_tests()


def test_micro_risk_scales_with_size():
    with patch("trading.open_position_view.point_value_gbp_for_epic", return_value=0.8):
        tp, sl, profile = resolve_micro_tp_sl_for_epic(
            "CS.D.CFPGOLD.CFP.IP", 1.0, {"micro_risk": {"risk_per_trade_gbp": 5.0}}
        )
    assert sl > 0
    assert tp >= sl
    assert profile.risk_per_trade_gbp == 5.0
    with patch("trading.open_position_view.point_value_gbp_for_epic", return_value=0.8):
        tp10, sl10, _ = resolve_micro_tp_sl_for_epic(
            "CS.D.CFPGOLD.CFP.IP", 10.0, {"micro_risk": {"risk_per_trade_gbp": 5.0}}
        )
    assert sl10 < sl


def test_ig_budget_snapshot_fields():
    snap = ig_budget_snapshot()
    assert "calls_last_30m" in snap
    assert "rate_limited" in snap
    assert "estimated_budget_remaining" in snap


def test_execution_guard_blocks_when_rate_limited():
    from execution.ig_execution_guard import ig_execution_allowed

    with patch("system.ig_budget_monitor.execution_paused", return_value=True):
        allowed, reason = ig_execution_allowed()
    assert allowed is False
    assert "ig_rate_limited" in reason
