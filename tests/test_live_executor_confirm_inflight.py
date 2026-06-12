"""LiveExecutor — inflight / pending confirm must block duplicate entry."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.cooldown_tracker import CooldownTracker
from execution.entry_inflight import (
    has_entry_in_flight,
    reset_entry_inflight_state_for_tests,
    try_begin_entry,
)
from execution.live_executor import LiveExecutor
from execution.pending_order_reconcile import (
    ORDER_TYPE_ENTRY,
    has_pending,
    mark_pending,
    reset_pending_state_for_tests,
    resolve_pending,
)
from execution.trade_manager import TradeManager
from execution.types import ExecutionMode, ExecutionResult, TradeSignal


def _signal(direction: str = "BUY") -> TradeSignal:
    q = Quote(datetime(2026, 5, 27, 12, 0), 65000.0, 65007.0)
    return TradeSignal(
        market="Japan 225",
        epic="IX.D.NIKKEI.IFM.IP",
        direction=direction,
        raw_confidence=92.0,
        adjusted_confidence=92.0,
        setup_key="test|inflight",
        quote=q,
        notes="inflight test",
    )


def _params() -> dict:
    return {"size": 1.0, "risk": 40.0, "limit": 80.0}


class LiveExecutorConfirmInflightTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        try:
            from system.rate_limit_manager import get_rate_limit_manager

            get_rate_limit_manager().reset_for_tests()
        except Exception:
            pass

    def tearDown(self) -> None:
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()

    def test_try_begin_entry_blocks_duplicate(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        self.assertTrue(try_begin_entry(epic, "BUY", 1.0))
        self.assertFalse(try_begin_entry(epic, "BUY", 1.0))

    @patch("system.rate_limit_manager.get_rate_limit_manager")
    @patch("execution.live_executor.japan225_daily_risk_paused", return_value=False)
    def test_second_entry_blocked_while_confirm_worker_hangs(
        self, _risk_pause: MagicMock, rate_mgr: MagicMock
    ) -> None:
        """Simulates confirm_deal hang: worker stuck until release; no second POST."""
        rate_mgr.return_value.check_rest_allowed.return_value = None
        release_worker = threading.Event()
        epic = "IX.D.NIKKEI.IFM.IP"

        cfg = MagicMock()
        cfg.allow_live_trading = True
        cfg.dry_run = False
        cfg.trade_size = 1.0
        cfg.stop_distance_points = 40.0
        cfg.limit_distance_points = 80.0
        cfg.currency_code = "GBP"
        cfg.max_retries = 1
        cfg.retry_delay_seconds = 5.0
        cfg.account_type = "DEMO"

        client = MagicMock()
        client.account_type = "DEMO"
        client._base = "https://demo-api.ig.com"
        client.account_id = "ACC"

        executor = LiveExecutor(cfg, client)
        trade_mgr = MagicMock(spec=TradeManager)
        cooldown = MagicMock(spec=CooldownTracker)

        def blocking_confirm_path(*_a, **_k) -> ExecutionResult:
            release_worker.wait(timeout=5.0)
            return ExecutionResult(
                success=True,
                action="EXECUTED",
                deal_reference="REF-1",
                deal_id="DI-1",
            )

        with patch.object(
            executor, "_execute_order_blocking", side_effect=blocking_confirm_path
        ):
            first = executor.execute(
                _signal("BUY"),
                _params(),
                trade_mgr,
                cooldown,
                mode=ExecutionMode.DEMO,
            )
            self.assertEqual(first.action, "SUBMITTED")

            deadline = time.time() + 2.0
            while time.time() < deadline and not has_entry_in_flight(epic):
                time.sleep(0.02)
            self.assertTrue(
                has_entry_in_flight(epic),
                "entry inflight should be set while worker blocks (confirm hang)",
            )

            second = executor.execute(
                _signal("BUY"),
                _params(),
                trade_mgr,
                cooldown,
                mode=ExecutionMode.DEMO,
            )
            self.assertFalse(second.success)
            self.assertIn("in flight", (second.rejection_reason or "").lower())

        release_worker.set()
        executor.wait_pending_orders(timeout=10.0)
        self.assertFalse(has_entry_in_flight(epic))

    @patch("system.rate_limit_manager.get_rate_limit_manager")
    @patch("execution.live_executor.japan225_daily_risk_paused", return_value=False)
    def test_has_pending_blocks_new_entry_after_unresolved_confirm(
        self, _risk_pause: MagicMock, rate_mgr: MagicMock
    ) -> None:
        rate_mgr.return_value.check_rest_allowed.return_value = None
        epic = "IX.D.NIKKEI.IFM.IP"
        mark_pending(
            epic, side="BUY", order_type=ORDER_TYPE_ENTRY, deal_reference="REF-X"
        )
        self.assertTrue(has_pending(epic))

        cfg = MagicMock()
        cfg.allow_live_trading = True
        cfg.dry_run = False
        cfg.account_type = "DEMO"

        client = MagicMock()
        client.account_type = "DEMO"
        executor = LiveExecutor(cfg, client)

        result = executor.execute(
            _signal("BUY"),
            _params(),
            MagicMock(),
            MagicMock(),
            mode=ExecutionMode.DEMO,
        )
        self.assertFalse(result.success)
        reason = (result.rejection_reason or "").lower()
        self.assertTrue(
            "confirmation" in reason or "paused" in reason,
            result.rejection_reason,
        )
        client.place_market_order.assert_not_called()
        resolve_pending(epic, reason="test cleanup")


if __name__ == "__main__":
    unittest.main()
