"""Tests for deploy hold informational gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from runtime.deploy_hold import (
    deploy_hold_config_active,
    is_deploy_hold_active,
    warn_if_deploy_window_closed,
)


def test_deploy_hold_config_default():
    cfg = {"desk_deploy": {"hold_active_session": True}}
    assert deploy_hold_config_active(cfg) is True
    assert is_deploy_hold_active(cfg) is True


@patch("runtime.deploy_hold.broker_open_count", return_value=2)
@patch("runtime.deploy_hold.log_engine")
def test_warn_on_active_session(mock_log, _mock_opens):
    cfg = {"desk_deploy": {"hold_active_session": True}}
    closed = warn_if_deploy_window_closed(MagicMock(), cfg=cfg)
    assert closed is True
    mock_log.assert_called_once()
    assert "deploy window CLOSED" in mock_log.call_args[0][0]


@patch("runtime.deploy_hold.broker_open_count", return_value=0)
@patch("runtime.deploy_hold.log_engine")
def test_no_warn_when_flat(mock_log, _mock_opens):
    cfg = {"desk_deploy": {"hold_active_session": True}}
    assert warn_if_deploy_window_closed(MagicMock(), cfg=cfg) is False
    mock_log.assert_not_called()
