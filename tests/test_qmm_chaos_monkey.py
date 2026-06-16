"""
QMM chaos monkey — deliberate infrastructure failures during active trading.

Injects REST dropout, LearningStore lock contention, and corrupted hub quotes
while asserting exception shields, thread survival, and portfolio rollback.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from data.models import Quote
from execution.cooldown_tracker import CooldownTracker
from execution.entry_inflight import reset_entry_inflight_state_for_tests
from execution.live_executor import LiveExecutor
from execution.order_reconciler_worker import reconcile_all_pending_orders
from execution.pending_order_reconcile import (
    get_pending,
    has_pending,
    reset_pending_state_for_tests,
)
from execution.trade_manager import TradeManager
from execution.types import ExecutionMode, ExecutionResult, TradeSignal
from ml.interim_scorer import reset_ml_clean_training_rows_cache_for_tests
from system.market_data_hub import get_market_data_hub
from system.portfolio_envelope import (
    reset_portfolio_envelope_for_tests,
    snapshot as portfolio_snapshot,
)
from trading.points_engine import (
    PointsEngine,
    flush_points_persist,
    reset_points_persist_for_tests,
    set_points_state_path_for_tests,
)
from trading.trading_loop import TradingLoop

from test_qmm_unified_pipeline import (
    _fresh_quote,
    _make_trading_loop,
)

HERO_EPIC = "IX.D.NIKKEI.IFM.IP"


def _trade_signal() -> TradeSignal:
    q = _fresh_quote()
    return TradeSignal(
        market="Japan 225",
        epic=HERO_EPIC,
        direction="BUY",
        raw_confidence=92.0,
        adjusted_confidence=92.0,
        setup_key="chaos|qmm",
        quote=q,
        notes="chaos monkey",
    )


def _execution_params() -> dict:
    return {
        "size": 1.0,
        "risk": 40.0,
        "limit": 80.0,
        "risk_gbp": 40.0,
        "gate_sourced": True,
    }


def _live_executor() -> LiveExecutor:
    cfg = MagicMock()
    cfg.allow_live_trading = True
    cfg.dry_run = False
    cfg.trade_size = 1.0
    cfg.stop_distance_points = 40.0
    cfg.limit_distance_points = 80.0
    cfg.currency_code = "GBP"
    cfg.max_retries = 0
    cfg.retry_delay_seconds = 0.05
    cfg.account_type = "DEMO"
    cfg.get = lambda k, d=None: 1.0 if k == "ig_point_value_gbp" else d
    client = MagicMock()
    client.account_type = "DEMO"
    client._base = "https://demo-api.ig.com"
    client.account_id = "ACC"
    return LiveExecutor(cfg, client)


class QmmChaosMonkeyTests(unittest.TestCase):
    """Unified QMM pipeline under injected real-world failure modes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self._tmp.name) / "points.json")
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        reset_portfolio_envelope_for_tests()
        reset_ml_clean_training_rows_cache_for_tests()
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
        reset_points_persist_for_tests()
        set_points_state_path_for_tests(None)
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        reset_portfolio_envelope_for_tests()
        reset_ml_clean_training_rows_cache_for_tests()
        self._tmp.cleanup()

    def test_rest_dropout_mid_dispatch_reconciler_releases_capital(self) -> None:
        """Socket dropout during REST order — pending reconcile + scavenger rollback."""
        executor = _live_executor()
        client = executor._client
        client.confirm_deal.return_value = {
            "accepted": False,
            "rejected": True,
            "reason": "broker rejected after timeout",
            "deal_id": None,
        }
        client.open_positions.return_value = []
        client.has_open_position.return_value = False

        poll_alive = threading.Event()
        poll_alive.set()

        def _tick_loop() -> None:
            while poll_alive.is_set():
                time.sleep(0.01)

        poll_thread = threading.Thread(target=_tick_loop, name="chaos-poll-shield")
        poll_thread.start()

        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as rate_mgr,
            patch(
                "execution.live_executor.japan225_daily_risk_paused",
                return_value=False,
            ),
            patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=True),
            patch("system.portfolio_envelope.can_allocate", return_value=(True, "ok")),
            patch("system.portfolio_envelope.release_allocation") as mock_release,
            patch.object(
                executor,
                "_execute_order_blocking",
                return_value=ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason="socket dropout mid REST",
                    deal_reference="REF-CHAOS-DROP",
                    execution_params=_execution_params(),
                ),
            ),
        ):
            rate_mgr.return_value.check_rest_allowed.return_value = None
            result = executor.execute(
                _trade_signal(),
                _execution_params(),
                MagicMock(spec=TradeManager),
                MagicMock(spec=CooldownTracker),
                mode=ExecutionMode.DEMO,
            )
            self.assertEqual(result.action, "SUBMITTED")
            executor.wait_pending_orders(timeout=5.0)

            mock_release.assert_not_called()
            self.assertTrue(has_pending(HERO_EPIC))
            pending = get_pending(HERO_EPIC)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertTrue(pending.pending_reconcile)

            cleared = reconcile_all_pending_orders(client, config=executor._cfg)
            self.assertEqual(cleared, 1)
            mock_release.assert_called()
            self.assertFalse(has_pending(HERO_EPIC))
        self.assertTrue(poll_thread.is_alive())
        poll_alive.clear()
        poll_thread.join(timeout=2.0)

    def test_learning_store_lock_does_not_kill_gate_thread(self) -> None:
        """SQLite lock on LearningStore — ML gate shield returns safe default."""
        loop = _make_trading_loop()
        errors: list[str] = []

        def _locked_ml(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        def _run_tick() -> None:
            try:
                with patch(
                    "ml.interim_scorer.ml_clean_training_rows",
                    side_effect=_locked_ml,
                ):
                    results = loop._evaluate_gates_core(_fresh_quote())
                self.assertIsInstance(results, list)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        workers = [threading.Thread(target=_run_tick) for _ in range(4)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=5.0)
        self.assertEqual(errors, [], f"gate thread escaped shield: {errors}")

    def test_corrupted_hub_quotes_trapped_by_market_shields(self) -> None:
        """Erratic hub quotes must not drop market polling threads."""
        hub = get_market_data_hub()
        loop = _make_trading_loop()
        crashes: list[str] = []
        bad_packets = [
            (HERO_EPIC, -1.0, 0.0),
            (HERO_EPIC, float("nan"), 1.0),
            (HERO_EPIC, 1.0, -5.0),
            (HERO_EPIC, 999999.0, 0.01),
        ]

        def _poll_loop() -> None:
            for epic, bid, offer in bad_packets:
                try:
                    hub.publish(epic, bid, offer, source="chaos")
                    loop._evaluate_gates_core(
                        Quote(datetime.now(timezone.utc), max(bid, 0.01), max(offer, 0.02))
                    )
                except Exception as exc:
                    crashes.append(str(exc))

        poll_thread = threading.Thread(target=_poll_loop, name="chaos-hub-poll")
        poll_thread.start()
        poll_thread.join(timeout=5.0)
        self.assertEqual(crashes, [], f"market thread dropped on bad quote: {crashes}")

    def test_points_hot_path_instant_memory_background_persist(self) -> None:
        """Post-trade reward updates RAM instantly; disk flush stays on worker cadence."""
        state_path = Path(self._tmp.name) / "points_hot.json"
        engine = PointsEngine(state_path=state_path)
        before = engine._cumulative
        engine.record_trade("WIN", 90.0, 12.0, pnl_gbp=15.0)
        self.assertGreater(engine._cumulative, before)
        self.assertFalse(state_path.exists())
        flush_points_persist()
        self.assertTrue(state_path.exists())

    def test_portfolio_allocation_rollback_after_definite_rejection(self) -> None:
        """Definite broker rejection releases capital on worker path (non-pending)."""
        executor = _live_executor()
        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as rate_mgr,
            patch(
                "execution.live_executor.japan225_daily_risk_paused",
                return_value=False,
            ),
            patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=True),
            patch("system.portfolio_envelope.release_allocation") as mock_release,
            patch("system.portfolio_envelope.can_allocate", return_value=(True, "ok")),
            patch.object(
                executor,
                "_execute_order_blocking",
                return_value=ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason="size limit",
                    execution_params=_execution_params(),
                ),
            ),
        ):
            rate_mgr.return_value.check_rest_allowed.return_value = None
            executor.execute(
                _trade_signal(),
                _execution_params(),
                MagicMock(spec=TradeManager),
                MagicMock(spec=CooldownTracker),
                mode=ExecutionMode.DEMO,
            )
            executor.wait_pending_orders(timeout=5.0)

        mock_release.assert_called_once()
        snap = portfolio_snapshot()
        self.assertAlmostEqual(snap["concurrent_risk_gbp"], 0.0)


if __name__ == "__main__":
    unittest.main()
