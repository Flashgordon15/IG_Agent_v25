"""Tests for live canary session baseline reset."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from runtime.live_canary_session import live_canary_enabled, reset_live_canary_session_gates


def test_live_canary_enabled():
    assert live_canary_enabled({"live_canary": {"enabled": True}}) is True
    assert live_canary_enabled({"live_canary": {"enabled": False}}) is False


@patch("system.v291_upgrade.refresh_today_daily_loss_baseline")
def test_reset_skipped_when_disabled(mock_baseline):
    store = MagicMock()
    out = reset_live_canary_session_gates(store, cfg={"live_canary": {"enabled": False}})
    assert out["applied"] is False
    mock_baseline.assert_not_called()


@patch("system.v291_upgrade.refresh_today_daily_loss_baseline")
def test_reset_clears_shield_when_enabled(mock_baseline):
    mock_baseline.return_value = {"baseline_pnl": 0.0, "effective_loss_gbp": 0.0}
    store = MagicMock()
    cfg = {"live_canary": {"enabled": True}}
    out = reset_live_canary_session_gates(store, cfg=cfg)
    assert out["applied"] is True
    assert store.set_runtime_state.call_count >= 4
