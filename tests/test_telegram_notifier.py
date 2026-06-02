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
                }
            }
        )
        n = configure_telegram(cfg)
        self.assertTrue(n.enabled)

    def test_send_now_calls_sync(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        with patch.object(n, "_send_sync") as sync:
            n.send_now("test message")
        sync.assert_called_once_with("test message")

    def test_notify_startup_uses_send_now(self) -> None:
        n = TelegramNotifier(enabled=True, bot_token="tok", chat_id="99")
        with patch.object(n, "send_now") as send_now:
            n.notify_startup(state_restored=True)
        send_now.assert_called_once()
        self.assertIn("restarted", send_now.call_args[0][0])

    def test_trade_open_message_format(self) -> None:
        n = TelegramNotifier(enabled=False, bot_token="", chat_id="")
        with patch.object(n, "_send_async") as send:
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
        text = send.call_args[0][0]
        self.assertIn("Trade Opened", text)
        self.assertIn("67,350", text)
        self.assertIn("91%", text)


if __name__ == "__main__":
    unittest.main()
