"""OrderConfirmWorker failure containment and portfolio release."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.cooldown_tracker import CooldownTracker
from execution.entry_inflight import reset_entry_inflight_state_for_tests
from execution.live_executor import LiveExecutor
from execution.pending_order_reconcile import reset_pending_state_for_tests
from execution.trade_manager import TradeManager
from execution.types import ExecutionMode, ExecutionResult, TradeSignal


def _signal() -> TradeSignal:
    q = Quote(datetime(2026, 6, 15, 12, 0), 65000.0, 65007.0)
    return TradeSignal(
        market="Japan 225",
        epic="IX.D.NIKKEI.IFM.IP",
        direction="BUY",
        raw_confidence=92.0,
        adjusted_confidence=92.0,
        setup_key="test|worker_fail",
        quote=q,
        notes="worker failure test",
    )


def _params() -> dict:
    return {
        "size": 1.0,
        "risk": 40.0,
        "limit": 80.0,
        "risk_gbp": 40.0,
        "gate_sourced": True,
    }


class OrderConfirmWorkerFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        try:
            from system.portfolio_envelope import reset_portfolio_envelope_for_tests

            reset_portfolio_envelope_for_tests()
        except Exception:
            pass
        try:
            from execution.portfolio_hooks import reset_portfolio_hooks_for_tests

            reset_portfolio_hooks_for_tests()
        except Exception:
            pass
        try:
            from system.rate_limit_manager import get_rate_limit_manager

            get_rate_limit_manager().reset_for_tests()
        except Exception:
            pass

    def tearDown(self) -> None:
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        try:
            from execution.portfolio_hooks import reset_portfolio_hooks_for_tests

            reset_portfolio_hooks_for_tests()
        except Exception:
            pass

    def _executor(self) -> LiveExecutor:
        cfg = MagicMock()
        cfg.allow_live_trading = True
        cfg.dry_run = False
        cfg.trade_size = 1.0
        cfg.stop_distance_points = 40.0
        cfg.limit_distance_points = 80.0
        cfg.currency_code = "GBP"
        cfg.max_retries = 0
        cfg.retry_delay_seconds = 0.1
        cfg.account_type = "DEMO"
        cfg.get = lambda k, d=None: 1.0 if k == "ig_point_value_gbp" else d
        client = MagicMock()
        client.account_type = "DEMO"
        client._base = "https://demo-api.ig.com"
        client.account_id = "ACC"
        return LiveExecutor(cfg, client)

    @patch("system.rate_limit_manager.get_rate_limit_manager")
    @patch("execution.live_executor.japan225_daily_risk_paused", return_value=False)
    @patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=True)
    @patch("system.portfolio_envelope.release_allocation")
    @patch("system.portfolio_envelope.can_allocate", return_value=(True, "ok"))
    def test_worker_rejection_releases_portfolio_reservation(
        self,
        _can: MagicMock,
        mock_release: MagicMock,
        _gate: MagicMock,
        _risk_pause: MagicMock,
        rate_mgr: MagicMock,
    ) -> None:
        rate_mgr.return_value.check_rest_allowed.return_value = None
        executor = self._executor()
        trade_mgr = MagicMock(spec=TradeManager)
        cooldown = MagicMock(spec=CooldownTracker)

        with patch.object(
            executor,
            "_execute_order_blocking",
            return_value=ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason="IG size limit",
                execution_params=_params(),
            ),
        ):
            result = executor.execute(
                _signal(),
                _params(),
                trade_mgr,
                cooldown,
                mode=ExecutionMode.DEMO,
            )
            self.assertEqual(result.action, "SUBMITTED")
            executor.wait_pending_orders(timeout=5.0)

        mock_release.assert_called_once()
        self.assertAlmostEqual(float(mock_release.call_args[0][0]), 40.0)

    @patch("system.rate_limit_manager.get_rate_limit_manager")
    @patch("execution.live_executor.japan225_daily_risk_paused", return_value=False)
    @patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=True)
    @patch("system.portfolio_envelope.release_allocation")
    def test_worker_exception_without_ref_releases_immediately(
        self,
        mock_release: MagicMock,
        _gate: MagicMock,
        _risk_pause: MagicMock,
        rate_mgr: MagicMock,
    ) -> None:
        rate_mgr.return_value.check_rest_allowed.return_value = None
        executor = self._executor()
        trade_mgr = MagicMock(spec=TradeManager)
        cooldown = MagicMock(spec=CooldownTracker)

        def _boom(*_a, **_k):
            raise ConnectionError("IG platform timeout")

        with patch.object(executor, "_execute_order_blocking", side_effect=_boom):
            result = executor.execute(
                _signal(),
                _params(),
                trade_mgr,
                cooldown,
                mode=ExecutionMode.DEMO,
            )
            self.assertTrue(result.success)
            self.assertEqual(result.action, "SUBMITTED")
            executor.wait_pending_orders(timeout=5.0)

        mock_release.assert_called_once()
        trade_mgr.open_trade_from_execution.assert_not_called()

    @patch("system.rate_limit_manager.get_rate_limit_manager")
    @patch("execution.live_executor.japan225_daily_risk_paused", return_value=False)
    @patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=True)
    @patch("system.portfolio_envelope.release_allocation")
    def test_worker_ambiguous_ref_defers_release_until_reconciler(
        self,
        mock_release: MagicMock,
        _gate: MagicMock,
        _risk_pause: MagicMock,
        rate_mgr: MagicMock,
    ) -> None:
        rate_mgr.return_value.check_rest_allowed.return_value = None
        executor = self._executor()
        client = executor._client
        client.confirm_deal.return_value = {
            "accepted": False,
            "rejected": True,
            "reason": "confirm timeout",
        }
        client.open_positions.return_value = []
        client.has_open_position.return_value = False
        trade_mgr = MagicMock(spec=TradeManager)
        cooldown = MagicMock(spec=CooldownTracker)

        with patch.object(
            executor,
            "_execute_order_blocking",
            return_value=ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason="socket dropout mid confirm",
                deal_reference="REF-MID-DROP",
                execution_params=_params(),
            ),
        ):
            result = executor.execute(
                _signal(),
                _params(),
                trade_mgr,
                cooldown,
                mode=ExecutionMode.DEMO,
            )
            self.assertEqual(result.action, "SUBMITTED")
            executor.wait_pending_orders(timeout=5.0)

        mock_release.assert_not_called()
        from execution.order_reconciler_worker import reconcile_all_pending_orders

        cleared = reconcile_all_pending_orders(client, config=executor._cfg)
        self.assertEqual(cleared, 1)
        mock_release.assert_called_once()

    @patch("system.rate_limit_manager.get_rate_limit_manager")
    @patch("execution.live_executor.japan225_daily_risk_paused", return_value=False)
    def test_execute_returns_before_blocking_rest(
        self,
        _risk_pause: MagicMock,
        rate_mgr: MagicMock,
    ) -> None:
        rate_mgr.return_value.check_rest_allowed.return_value = None
        executor = self._executor()
        client = executor._client
        client.place_market_order = MagicMock(side_effect=AssertionError("no sync REST"))

        with patch.object(
            executor,
            "_execute_order_blocking",
            return_value=ExecutionResult(
                success=True,
                action="EXECUTED",
                deal_reference="REF-OK",
                deal_id="DEAL-OK",
            ),
        ):
            result = executor.execute(
                _signal(),
                _params(),
                MagicMock(spec=TradeManager),
                MagicMock(spec=CooldownTracker),
                mode=ExecutionMode.DEMO,
            )

        self.assertEqual(result.action, "SUBMITTED")
        client.place_market_order.assert_not_called()
        executor.wait_pending_orders(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
