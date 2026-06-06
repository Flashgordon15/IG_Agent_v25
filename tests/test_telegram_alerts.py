"""Tests for critical Telegram alert wiring."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import agent_control  # noqa: E402
from system.telegram_notifier import send_critical_alert  # noqa: E402


def test_critical_alert_function_exists() -> None:
    assert callable(send_critical_alert)


@patch("system.telegram_notifier.get_telegram_notifier")
def test_send_critical_alert_prefixes_and_sends(mock_get: MagicMock) -> None:
    notifier = MagicMock()
    notifier.enabled = True
    notifier.send_now.return_value = True
    mock_get.return_value = notifier

    ok = send_critical_alert("test failure")

    assert ok is True
    text = notifier.send_now.call_args[0][0]
    assert "IG Agent CRITICAL" in text
    assert "test failure" in text


@patch("system.telegram_notifier.send_critical_alert")
def test_shutdown_sends_telegram(mock_alert: MagicMock) -> None:
    from main import AgentRuntime

    runtime = AgentRuntime()
    runtime.shutdown(source="test")

    mock_alert.assert_called_once_with("🛑 Agent stopped (source: test)")


@patch("system.telegram_notifier.send_critical_alert")
def test_trading_loop_stop_sends_alert(mock_alert: MagicMock) -> None:
    loop = MagicMock()
    loop.is_running.return_value = True
    loop.stop = MagicMock()

    with (
        patch.object(agent_control, "_loop", loop),
        patch.object(agent_control, "_paused", False),
    ):
        result = agent_control.stop_trading()

    assert result["ok"] is True
    mock_alert.assert_called_once_with("⚠️ Trading loops STOPPED — no trades firing")
