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
from trading.trading_loop import TradingLoop


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
        loop._env.score.side_effect = RuntimeError("boom")
        ctx = loop.run_once()
        assert ctx is not None
        self.assertFalse(ctx.all_passed)
        env_gate = next(g for g in ctx.gates if g.name == "environment_fitness")
        self.assertFalse(env_gate.passed)
        self.assertIn("boom", env_gate.detail)

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
    def test_flatten_invoked_when_should_flatten(self, _snap: MagicMock) -> None:
        flatten = MagicMock(return_value=2)
        loop = _make_loop(on_flatten=flatten)
        loop._session.should_flatten.return_value = True
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


if __name__ == "__main__":
    unittest.main()
