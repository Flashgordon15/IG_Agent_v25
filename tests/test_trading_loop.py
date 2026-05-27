"""Tests for trading.trading_loop orchestration — Section 4.5 Step 9."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.trading_loop import TickOutcome
from signals.signal_engine import SignalResult
from trading.trading_loop import GateResult, TradingLoop, signal_gate_explanation


def _quote() -> Quote:
    return Quote(datetime(2026, 5, 27, 1, 0), 100.0, 100.5)


def _wait_signal() -> SignalResult:
    return SignalResult(
        signal="WAIT",
        raw_confidence=0.0,
        adjusted_confidence=0.0,
        learning_delta=0.0,
        setup_key="",
        notes="wait",
        snapshot={},
    )


def _buy_signal(conf: float = 90.0) -> SignalResult:
    return SignalResult(
        signal="BUY",
        raw_confidence=conf,
        adjusted_confidence=conf,
        learning_delta=0.0,
        setup_key="test",
        notes="",
        snapshot={"atr": 50.0},
    )


def _make_loop(**overrides) -> TradingLoop:
    config = MagicMock()
    config.refresh_seconds = 0.05
    config.max_spread_points = 35.0
    config.stop_distance_points = 40.0
    config.trade_size = 1.0
    config.currency_code = "GBP"
    config.get = MagicMock(return_value=1.0)

    session = MagicMock()
    session.is_session_open.return_value = True
    session.is_cold_start.return_value = False
    session.check_gap_open.return_value = False
    session.bars_since_open.return_value = 10
    session.should_flatten.return_value = False
    session.should_run_flatten_attempt.return_value = False
    session.is_entry_blocked_near_session_end.return_value = (False, None)
    session.on_tick = MagicMock()

    env = MagicMock()
    env.score.return_value = 55.0

    points = MagicMock()
    points.get_state.return_value = "HEALTHY"
    points.is_session_paused.return_value = False
    points.is_day_stopped.return_value = False
    points.get_threshold.return_value = 80.0
    points.get_size_multiplier.return_value = 1.0
    points.snapshot.return_value = MagicMock(
        cumulative=0.0,
        session_score=0.0,
        last_trade_score=0.0,
    )

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = _wait_signal()
    signal_engine.quote_df.return_value = None

    exec_engine = MagicMock()
    exec_engine.trade_tracker.count_open_for_epic.return_value = 0
    exec_engine.trade_tracker.snapshot.return_value = {"positions": []}
    exec_engine.update_positions = MagicMock()

    execution_loop = MagicMock()
    execution_loop.execution_engine = exec_engine
    execution_loop.process_tick = MagicMock(
        return_value=TickOutcome(
            quote=_quote(),
            signal=_wait_signal(),
            trade_signal=MagicMock(),
            validation=MagicMock(allowed=False, reasons=[], checks={}),
        )
    )

    store = MagicMock()
    store.sum_daily_pnl.return_value = 0.0

    kwargs = {
        "config": config,
        "market": "Japan 225",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "session_manager": session,
        "environment_scorer": env,
        "points_engine": points,
        "signal_engine": signal_engine,
        "execution_loop": execution_loop,
        "quote_source": lambda: _quote(),
        "learning_store": store,
        "tick_interval_sec": 0.05,
    }
    kwargs.update(overrides)
    return TradingLoop(**kwargs)


class SignalGateExplanationTests(unittest.TestCase):
    def test_rsi_block_shows_reason_and_score(self) -> None:
        sig = SignalResult(
            signal="WAIT",
            raw_confidence=86.0,
            adjusted_confidence=86.0,
            learning_delta=0.0,
            setup_key="BUY|bull|atr0-30|rsihigh",
            notes="blocked: RSI overbought filter: 72.0 > max 68",
            snapshot={
                "raw_signal": "BUY",
                "rsi_block": "RSI overbought filter: 72.0 > max 68",
            },
        )
        detail, reason = signal_gate_explanation(sig, 80.0)
        self.assertIn("RSI overbought", detail)
        self.assertIn("BUY", detail)
        self.assertIn("86", detail)
        self.assertEqual(reason, "RSI overbought filter: 72.0 > max 68")

    def test_buy_below_threshold(self) -> None:
        sig = _buy_signal(75.0)
        detail, reason = signal_gate_explanation(sig, 80.0)
        self.assertIn("below", detail)
        self.assertIn("75", detail)


class PointsSkipConsumeTests(unittest.TestCase):
    def test_consume_skip_when_paused_and_signal_would_trade(self) -> None:
        loop = _make_loop()
        loop._points.is_session_paused.return_value = True
        loop._points.consume_signal_skip.return_value = True
        gates = [
            GateResult("points_state", False, detail="session pause"),
            GateResult("signal_confidence", True, detail="BUY 90%"),
        ]
        loop._maybe_consume_points_skip_on_suppressed_signal(gates)
        loop._points.consume_signal_skip.assert_called_once()

    def test_no_consume_when_points_gate_passes(self) -> None:
        loop = _make_loop()
        loop._points.is_session_paused.return_value = False
        gates = [
            GateResult("points_state", True),
            GateResult("signal_confidence", True),
        ]
        loop._maybe_consume_points_skip_on_suppressed_signal(gates)
        loop._points.consume_signal_skip.assert_not_called()


class TradingLoopTests(unittest.TestCase):
    def test_gate_order_stops_at_first_failure(self) -> None:
        loop = _make_loop()
        loop._session.is_session_open.return_value = False
        ctx = loop.run_once()
        assert ctx is not None
        self.assertFalse(ctx.all_passed)
        loop._execution_loop.process_tick.assert_not_called()
        failed = [g for g in ctx.gates if not g.passed]
        self.assertEqual(failed[0].name, "session_open")

    def test_gate_safe_default_on_exception(self) -> None:
        loop = _make_loop()
        loop._env.score.side_effect = RuntimeError("scorer unavailable")
        ctx = loop.run_once()
        assert ctx is not None
        env_gate = next(g for g in ctx.gates if g.name == "environment_fitness")
        self.assertTrue(env_gate.passed)
        self.assertIn("50%", env_gate.detail)
        self.assertNotIn("scorer unavailable", env_gate.detail)

    def test_process_tick_when_all_gates_pass(self) -> None:
        loop = _make_loop()
        loop._signal_engine.evaluate.return_value = _buy_signal(92.0)
        ctx = loop.run_once()
        assert ctx is not None
        self.assertTrue(ctx.all_passed)
        loop._execution_loop.process_tick.assert_called_once()

    @patch("trading.trading_loop.publish_tick")
    def test_snapshot_written_every_tick(self, snap_mock: MagicMock) -> None:
        loop = _make_loop()
        loop.run_once()
        snap_mock.assert_called_once()
        payload = snap_mock.call_args[0][0]
        self.assertEqual(payload["type"], "tick")
        self.assertIn("health", payload)

    @patch("trading.trading_loop.publish_tick")
    @patch("trading.trading_loop.time.sleep")
    def test_flatten_invoked_when_attempt_due(
        self, _sleep: MagicMock, _snap: MagicMock
    ) -> None:
        flatten = MagicMock(return_value=2)
        sync = MagicMock()
        sync.count_for_epic.return_value = 0
        loop = _make_loop(on_flatten=flatten, position_sync=sync)
        loop._session.should_run_flatten_attempt.return_value = True
        loop._session.mark_flatten_attempt.return_value = 5.0
        loop.run_once()
        flatten.assert_called_once()
        loop._execution_loop.process_tick.assert_not_called()

    def test_start_stop_lifecycle(self) -> None:
        loop = _make_loop()
        loop.start()
        self.assertTrue(loop.is_running())
        time.sleep(0.12)
        loop.stop()
        self.assertFalse(loop.is_running())

    def test_loop_survives_tick_exception(self) -> None:
        loop = _make_loop()
        loop._quote_source = MagicMock(side_effect=[_quote(), None])
        loop._session.on_tick.side_effect = [None, RuntimeError("tick fail")]
        loop.run_once()
        loop.run_once()
        self.assertIsNotNone(loop.last_context)

    @patch("trading.trading_loop.publish_tick")
    def test_snapshot_includes_account_metrics(self, snap_mock: MagicMock) -> None:
        loop = _make_loop()
        rest = MagicMock()
        rest.maybe_refresh_account_summary.return_value = {
            "balance": 10_000.0,
            "profit_loss": -12.5,
            "available": 9_500.0,
        }
        loop._execution_loop.execution_engine._rest_client = rest
        loop._store.recent_closed_trades.return_value = [
            {"result": "WIN"},
            {"result": "LOSS"},
            {"result": "WIN"},
        ]
        loop.run_once()
        payload = snap_mock.call_args[0][0]
        self.assertEqual(payload["balance_gbp"], 10_000.0)
        self.assertEqual(payload["daily_pnl_gbp"], -12.5)
        self.assertEqual(payload["win_rate_20"], 67)

    def test_daily_pnl_prefers_journal_when_open(self) -> None:
        loop = _make_loop()
        loop._store.sum_daily_pnl.return_value = 25.0
        rest = MagicMock()
        rest.maybe_refresh_account_summary.return_value = {"profit_loss": 99.0}
        loop._execution_loop.execution_engine._rest_client = rest
        pnl = loop._daily_pnl_signed_gbp([{"deal_id": "D1"}])
        self.assertEqual(pnl, 25.0)

    def test_positions_fallback_to_position_sync(self) -> None:
        loop = _make_loop()
        loop._execution_loop.execution_engine.trade_tracker.snapshot.return_value = {
            "positions": []
        }
        sync = MagicMock()
        sync.snapshot_dict.return_value = {
            "positions": [
                {
                    "deal_id": "IG1",
                    "epic": "IX.D.NIKKEI.IFM.IP",
                    "direction": "BUY",
                    "level": 65000.0,
                    "upl": 8.0,
                    "size": 1.0,
                }
            ]
        }
        loop._position_sync = sync
        rows = loop._positions_payload(_quote())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pnl_gbp"], 8.0)


if __name__ == "__main__":
    unittest.main()
