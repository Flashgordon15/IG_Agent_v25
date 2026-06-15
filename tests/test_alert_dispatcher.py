"""Tests for non-blocking alert dispatch."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from system.alert_dispatcher import (
    enqueue_critical_alert,
    flush_alert_dispatcher,
    reset_alert_dispatcher_for_tests,
    start_alert_dispatcher,
)


class AlertDispatcherTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_alert_dispatcher_for_tests()

    @patch("system.telegram_notifier.send_critical_alert", return_value=True)
    def test_enqueue_dispatches_on_background_worker(self, mock_send) -> None:
        start_alert_dispatcher()
        ok = enqueue_critical_alert("test alert", dedupe_key="unit_test")
        self.assertTrue(ok)
        flush_alert_dispatcher(timeout=2.0)
        mock_send.assert_called_once_with("test alert", dedupe_key="unit_test")

    def test_empty_message_not_queued(self) -> None:
        self.assertFalse(enqueue_critical_alert("   "))


if __name__ == "__main__":
    unittest.main()
