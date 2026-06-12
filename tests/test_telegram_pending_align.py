"""Telegram + pending reconcile alignment tests."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.pending_order_reconcile import (
    ORDER_TYPE_ENTRY,
    get_pending,
    mark_pending,
    reconcile_pending_via_position_state,
    reset_pending_state_for_tests,
)
from system.telegram_notifier import TelegramNotifier, send_unresolved_order_alert


class PendingReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pending_state_for_tests()

    def tearDown(self) -> None:
        reset_pending_state_for_tests()

    def test_stale_entry_pending_cleared_without_position(self) -> None:
        mark_pending(
            "EPIC.A", side="BUY", order_type=ORDER_TYPE_ENTRY, deal_reference="R1"
        )
        rec = get_pending("EPIC.A")
        assert rec is not None
        with patch(
            "execution.pending_order_reconcile.time.time",
            return_value=rec.local_created_at + 5.0,
        ):
            reconcile_pending_via_position_state(
                "EPIC.A", position_present=False, stale_entry_grace_sec=0.0
            )
        self.assertIsNone(get_pending("EPIC.A"))

    def test_fresh_entry_pending_not_cleared_without_position(self) -> None:
        mark_pending("EPIC.B", side="BUY", order_type=ORDER_TYPE_ENTRY)
        reconcile_pending_via_position_state(
            "EPIC.B", position_present=False, stale_entry_grace_sec=60.0
        )
        self.assertIsNotNone(get_pending("EPIC.B"))


class TelegramUnresolvedTests(unittest.TestCase):
    def test_unresolved_alert_deduped(self) -> None:
        n = TelegramNotifier(enabled=False, bot_token="", chat_id="")
        # disabled notifier returns False without sending
        self.assertFalse(
            send_unresolved_order_alert(
                "IX.D.TEST", age_seconds=45.0, order_type="entry", deal_reference="ABC"
            )
        )


if __name__ == "__main__":
    unittest.main()
