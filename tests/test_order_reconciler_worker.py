"""OrderReconcilerWorker scavenger — PENDING_RECONCILE broker verification."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.order_reconciler_worker import (
    OrderReconcilerWorker,
    reconcile_all_pending_orders,
    reset_order_reconciler_worker_for_tests,
)
from execution.pending_order_reconcile import (
    get_pending,
    mark_pending,
    reset_pending_state_for_tests,
)
from execution.portfolio_hooks import reset_portfolio_hooks_for_tests


class OrderReconcilerWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pending_state_for_tests()
        reset_portfolio_hooks_for_tests()
        reset_order_reconciler_worker_for_tests()

    def tearDown(self) -> None:
        reset_order_reconciler_worker_for_tests()
        reset_pending_state_for_tests()
        reset_portfolio_hooks_for_tests()

    def test_rejected_confirm_releases_stashed_allocation(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        params = {"risk_gbp": 55.0, "size": 1.0, "risk": 55.0}
        mark_pending(
            epic,
            side="BUY",
            order_type="entry",
            deal_reference="REF-CHAOS",
            pending_reconcile=True,
            execution_params=params,
        )
        client = MagicMock()
        client.confirm_deal.return_value = {
            "accepted": False,
            "rejected": True,
            "reason": "INSUFFICIENT_FUNDS",
        }
        client.open_positions.return_value = []
        client.has_open_position.return_value = False

        with unittest.mock.patch(
            "system.portfolio_envelope.portfolio_gate_enabled",
            return_value=True,
        ), unittest.mock.patch(
            "system.portfolio_envelope.release_allocation"
        ) as mock_release:
            cleared = reconcile_all_pending_orders(client)
        self.assertEqual(cleared, 1)
        self.assertIsNone(get_pending(epic))
        mock_release.assert_called_once()
        self.assertAlmostEqual(float(mock_release.call_args[0][0]), 55.0)

    def test_accepted_confirm_clears_pending_without_release(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        params = {"risk_gbp": 30.0, "size": 1.0, "risk": 30.0}
        mark_pending(
            epic,
            side="BUY",
            order_type="entry",
            deal_reference="REF-OK",
            pending_reconcile=True,
            execution_params=params,
        )
        client = MagicMock()
        client.confirm_deal.return_value = {
            "accepted": True,
            "rejected": False,
            "deal_id": "DEAL-123",
        }
        client.get_position_otc.return_value = {"position": {"dealId": "DEAL-123"}}

        with unittest.mock.patch(
            "system.portfolio_envelope.release_allocation"
        ) as mock_release:
            cleared = reconcile_all_pending_orders(client)
        self.assertEqual(cleared, 1)
        self.assertIsNone(get_pending(epic))
        mock_release.assert_not_called()

    def test_worker_tick_runs_under_guard(self) -> None:
        client = MagicMock()
        worker = OrderReconcilerWorker(client, interval_seconds=60.0)
        self.assertEqual(worker.tick_once(), 0)


if __name__ == "__main__":
    unittest.main()
