"""Tests for live canary cross-path guards."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from runtime.live_canary_guards import (
    canary_forex_hot_path_locked,
    canary_micro_dispatch_risk_ok,
    canary_path_a_epic_allowed,
)


CANARY_CFG = {
    "live_canary": {"enabled": True},
    "dual_core": {"forex_rotation_locked": True},
    "max_daily_loss_gbp": 5.0,
}


def test_canary_forex_lock_detected():
    assert canary_forex_hot_path_locked(CANARY_CFG) is True
    assert canary_forex_hot_path_locked({"live_canary": {"enabled": False}}) is False


@patch("runtime.dual_core_execution.epic_allowed_on_hot_path")
def test_path_a_blocks_non_hot_epic(mock_allowed):
    mock_allowed.return_value = False
    ok, reason = canary_path_a_epic_allowed("IX.D.DOW.IFM.IP", CANARY_CFG)
    assert ok is False
    assert reason == "canary_hot_path_only"


@patch("runtime.dual_core_execution.epic_allowed_on_hot_path")
def test_path_a_allows_hot_epic(mock_allowed):
    mock_allowed.return_value = True
    ok, reason = canary_path_a_epic_allowed("CS.D.EURUSD.CFD.IP", CANARY_CFG)
    assert ok is True
    assert reason == ""


@patch("trading.manual_intervention.entries_blocked_by_shield", return_value=(False, ""))
@patch("system.daily_loss_policy.daily_loss_gate_status", return_value=(False, "hard_limit", {}))
def test_micro_dispatch_blocked_on_daily_loss(mock_loss, mock_shield):
    store = MagicMock()
    ok, reason = canary_micro_dispatch_risk_ok(store, CANARY_CFG)
    assert ok is False
    assert "canary_daily_loss" in reason


def test_micro_dispatch_passes_when_canary_disabled():
    ok, reason = canary_micro_dispatch_risk_ok(None, {"live_canary": {"enabled": False}})
    assert ok is True
    assert reason == ""
