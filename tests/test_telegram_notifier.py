"""Tests for system.telegram_notifier."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.telegram_notifier import TelegramNotifier, configure_telegram


class TelegramNotifierTests(unittest.TestCase):
    def test_disabled_when_no_token(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="", chat_id="123")
        self.assertFalse(n.enabled)

    @patch("system.telegram_notifier.threading.Thread")
    def test_send_async_when_enabled(self, thread_mock) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        n._send_async("hello")
        thread_mock.assert_called_once()

    def test_configure_from_config(self) -> None:
        from system.config import Config

        cfg = Config(
            _data={
                "telegram": {
                    "enabled": True,
                    "bot_token": "abc",
                    "chat_id": "1",
                    "telegram_heartbeat_interval_seconds": 1800,
                    "telegram_substantial_loss_points": 40.0,
                }
            }
        )
        with patch("system.telegram_notifier.start_telegram_heartbeat"):
            n = configure_telegram(cfg)
        self.assertTrue(n.enabled)
        self.assertEqual(n.heartbeat_interval_seconds, 1800.0)
        self.assertEqual(n.substantial_loss_points, 40.0)

    def test_send_now_calls_sync(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        with patch.object(n, "_send_sync") as sync:
            n.send_now("test message")
        sync.assert_called_once_with("test message")

    def test_notify_startup_uses_send_now(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        with (
            patch(
                "system.telegram_notifier.executive_status_only_enabled",
                return_value=False,
            ),
            patch.object(n, "send_now") as send_now,
        ):
            n.notify_startup(state_restored=True)
        send_now.assert_called_once()
        text = send_now.call_args[0][0]
        self.assertIn("IG Agent v25 started", text)

    def test_trade_open_buffers_instead_of_immediate_send(self) -> None:
        n = TelegramNotifier(enabled=False, bot_token="", chat_id="")
        with (
            patch.object(n, "_send_async") as send,
            patch.object(n, "_buffer_alert") as buf,
        ):
            n.notify_trade_opened(
                market="Japan 225",
                direction="BUY",
                entry=67350.0,
                size=1.0,
                stop=67309.0,
                target=67432.0,
                signal_pct=91.0,
                fitness_pct=52.0,
            )
        send.assert_not_called()
        buf.assert_called_once()
        text = buf.call_args[0][0]
        self.assertIn("Japan 225", text)
        self.assertIn("BUY", text)
        self.assertIn("67,350", text)
        self.assertIn("91%", text)

    def test_risk_shield_bypasses_buffer(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        with (
            patch.object(n, "_buffer_alert") as buf,
            patch.object(n, "_alert_deduped") as deduped,
        ):
            n.send_alert(
                "[RISK SHIELD] IX.D.NIKKEI.IFM.IP Entry Blocked.",
                dedupe_key="risk:1",
            )
        buf.assert_not_called()
        deduped.assert_called_once()

    def test_minor_loss_buffered_substantial_loss_urgent(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99", substantial_loss_points=50.0)
        with patch.object(n, "_buffer_alert") as buf, patch.object(n, "_send_async") as send:
            n.notify_trade_closed(
                market="Japan 225",
                direction="BUY",
                entry=100.0,
                exit_price=99.0,
                pnl_gbp=None,
                pnl_pts=-12.0,
                duration_mins=5.0,
                points_before=0.0,
                points_after=-12.0,
                points_state="CAUTION",
            )
        buf.assert_called_once()
        send.assert_not_called()

        with patch.object(n, "_buffer_alert") as buf, patch.object(n, "_send_async") as send:
            n.notify_trade_closed(
                market="Japan 225",
                direction="BUY",
                entry=100.0,
                exit_price=90.0,
                pnl_gbp=None,
                pnl_pts=-55.0,
                duration_mins=5.0,
                points_before=0.0,
                points_after=-55.0,
                points_state="CAUTION",
            )
        buf.assert_not_called()
        send.assert_called_once()

    def test_flush_aggregated_heartbeat_format(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        n._heartbeat_buffer["session_pnl_points"] = 12.5
        n._heartbeat_buffer["suppressed_count"] = 7
        n._heartbeat_buffer["correlation_multiplier"] = 0.75
        with patch.object(n, "_send_async") as send:
            n.flush_aggregated_heartbeat({"positions": 2})
        text = send.call_args[0][0]
        self.assertIn("[AGENT HEARTBEAT REPORT]", text)
        self.assertIn("+12.5 pts", text)
        self.assertIn("0.75", text)
        self.assertIn("7 standard alerts", text)
        self.assertEqual(n._heartbeat_buffer["suppressed_count"], 0)


if __name__ == "__main__":
    unittest.main()
