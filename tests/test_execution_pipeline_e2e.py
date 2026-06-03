"""
End-to-end execution pipeline tests (mocked broker).

Proves: all 7 orchestrator gates pass -> execution process_tick -> execute_trade.
Does NOT place real IG orders (safe for CI).

For live DEMO routing validation (no order), run:
  PYTHONPATH=src python3 scripts/e2e_execution_probe.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.order_validator import ValidationResult
from execution.trading_loop import TickOutcome, TradingLoop as ExecutionTickLoop
from execution.types import ExecutionMode, ExecutionResult, TradeSignal
from signals.signal_engine import SignalResult
from trading.trading_loop import TradingLoop as OrchestratorLoop


def _quote() -> Quote:
    return Quote(datetime(2026, 5, 27, 12, 0), 65000.0, 65007.0)


def _buy_signal(conf: float = 92.0) -> SignalResult:
    return SignalResult(
        signal="BUY",
        raw_confidence=conf,
        adjusted_confidence=conf,
        learning_delta=0.0,
        setup_key="test|e2e",
        notes="e2e mock",
        snapshot={"atr": 50.0},
    )


def _make_orchestrator(**overrides) -> OrchestratorLoop:
    config = MagicMock()
    config.refresh_seconds = 0.05
    config.max_spread_points = 35.0
    config.max_positions_per_epic = 1
    config.max_open_positions = 3
    config.stop_distance_points = 45.0
    config.trade_size = 1.0
    config.currency_code = "GBP"
    config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "ig_point_value_gbp": 1.0,
            "risk_cap_gbp": 150,
        }.get(key, default)
    )

    session = MagicMock()
    session.is_session_open.return_value = True
    session.is_cold_start.return_value = False
    session.check_gap_open.return_value = False
    session.bars_since_open.return_value = 6
    session.should_flatten.return_value = False
    session.should_run_flatten_attempt.return_value = False
    session.is_entry_blocked_near_session_end.return_value = (False, None)
    session.snapshot.return_value = MagicMock(phase="OPEN")
    session.on_tick = MagicMock()

    env = MagicMock()
    env.score.return_value = 55.0

    points = MagicMock()
    points.get_state.return_value = "HEALTHY"
    points.is_session_paused.return_value = False
    points.is_day_stopped.return_value = False
    points.get_threshold.return_value = 80.0
    points.trade_confidence_threshold.return_value = 80.0
    points.get_size_multiplier.return_value = 1.0
    points.snapshot.return_value = MagicMock(
        cumulative=0.0, session_score=0.0, last_trade_score=0.0
    )

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = _buy_signal()
    signal_engine.quote_df.return_value = None
    signal_engine.add_quote = MagicMock()

    exec_engine = MagicMock()
    exec_engine.trade_tracker.count_open_for_epic.return_value = 0
    exec_engine.trade_tracker.count_open_total.return_value = 0
    exec_engine.trade_tracker.snapshot.return_value = {"positions": []}
    exec_engine.update_positions = MagicMock()

    execution_loop = MagicMock()
    execution_loop.execution_engine = exec_engine
    execution_loop.process_tick = MagicMock(
        return_value=TickOutcome(
            quote=_quote(),
            signal=_buy_signal(),
            trade_signal=MagicMock(),
            validation=ValidationResult(allowed=True, reasons=[], checks={}),
            execution=ExecutionResult(success=True, action="SUBMITTED"),
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
    return OrchestratorLoop(**kwargs)


class OrchestratorPipelineTests(unittest.TestCase):
    @patch("system.market_watch.japan225_session.japan225_strategy_paused", return_value=(False, ""))
    def test_all_seven_gates_pass_then_process_tick(self, _j225: MagicMock) -> None:
        loop = _make_orchestrator()
        ctx = loop.run_once()
        assert ctx is not None
        self.assertTrue(ctx.all_passed)
        self.assertEqual(len(ctx.gates), 7)
        self.assertTrue(all(g.passed for g in ctx.gates))
        loop._execution_loop.process_tick.assert_called_once()
        call = loop._execution_loop.process_tick.call_args
        self.assertEqual(call.args[:3], ("Japan 225", "IX.D.NIKKEI.IFM.IP", _quote()))
        self.assertIn("prefetched_signal", call.kwargs)


class ExecutionTickPipelineTests(unittest.TestCase):
    def test_process_tick_calls_execute_trade_when_valid(self) -> None:
        signal_engine = MagicMock()
        signal_engine.evaluate.return_value = _buy_signal(92.0)
        signal_engine.add_quote = MagicMock()

        exec_engine = MagicMock()
        exec_engine.mode = ExecutionMode.DEMO
        exec_engine.config.max_positions_per_epic = 1
        exec_engine.update_positions.return_value = []
        exec_engine.validate_only.return_value = ValidationResult(
            allowed=True, reasons=[], checks={"e2e": True}
        )
        exec_engine.margin_preflight.return_value = (True, "")
        exec_engine.execute_trade.return_value = ExecutionResult(
            success=True,
            action="SUBMITTED",
            messages=["mock submit"],
        )
        exec_engine.trade_tracker.count_open_for_epic.return_value = 0

        tick_loop = ExecutionTickLoop(
            signal_engine=signal_engine,
            execution_engine=exec_engine,
            auto_trade=True,
            live_gate=None,
            broker_connected=lambda: True,
        )

        with patch(
            "system.market_watch.japan225_session.japan225_strategy_paused",
            return_value=(False, ""),
        ):
            with patch("execution.live_executor.epic_has_pending_open", return_value=False):
                outcome = tick_loop.process_tick("Japan 225", "IX.D.NIKKEI.IFM.IP", _quote())

        self.assertIsNotNone(outcome.execution)
        self.assertTrue(outcome.execution.success)
        exec_engine.execute_trade.assert_called_once()
        call_signal = exec_engine.execute_trade.call_args[0][0]
        self.assertIsInstance(call_signal, TradeSignal)
        self.assertEqual(call_signal.direction, "BUY")

    def test_process_tick_skipped_when_auto_trade_off(self) -> None:
        signal_engine = MagicMock()
        signal_engine.evaluate.return_value = _buy_signal(92.0)
        signal_engine.add_quote = MagicMock()

        exec_engine = MagicMock()
        exec_engine.mode = ExecutionMode.DEMO
        exec_engine.config.max_positions_per_epic = 1
        exec_engine.update_positions.return_value = []
        exec_engine.validate_only.return_value = ValidationResult(allowed=True, reasons=[], checks={})
        exec_engine.margin_preflight.return_value = (True, "")
        exec_engine.trade_tracker.count_open_for_epic.return_value = 0

        tick_loop = ExecutionTickLoop(
            signal_engine=signal_engine,
            execution_engine=exec_engine,
            auto_trade=False,
            broker_connected=lambda: True,
        )

        with patch(
            "system.market_watch.japan225_session.japan225_strategy_paused",
            return_value=(False, ""),
        ):
            tick_loop.process_tick("Japan 225", "IX.D.NIKKEI.IFM.IP", _quote())

        exec_engine.execute_trade.assert_not_called()


class DryRunExecutorTests(unittest.TestCase):
    def test_dry_run_does_not_place_ig_order(self) -> None:
        from data.learning_store import LearningStore
        from execution.live_executor import LiveExecutor
        from system.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            cfg_data = {
                "operating_mode": "DEMO",
                "account_type": "DEMO",
                "epic": "IX.D.NIKKEI.IFM.IP",
                "dry_run": True,
                "allow_live_trading": False,
                "auto_trade_enabled": True,
                "signal_threshold": 85,
                "trade_size": 0.5,
                "risk_points": 40,
                "reward_multiple": 2.0,
                "limit_distance_points": 80,
                "stop_distance_points": 45,
                "max_spread": 35,
                "max_spread_points": 35,
                "fast_ema": 9,
                "slow_ema": 21,
                "rsi_period": 14,
                "rsi_buy_min": 58,
                "rsi_buy_max": 68,
                "rsi_sell_max": 45,
                "breakeven_enabled": True,
                "breakeven_trigger_points": 30,
                "breakeven_lock_points": 0,
                "breakeven_offset_points": 0,
                "max_open_positions": 1,
                "max_positions_per_epic": 1,
                "max_daily_loss_gbp": 200,
                "cooldown_seconds": 180,
                "learning_enabled": False,
                "max_live_quotes": 500,
            }
            cfg = Config(_data=cfg_data)
            store = LearningStore(str(Path(tmp) / "e2e.db"))
            store.connect()

            rest = MagicMock()
            rest.place_market_order = MagicMock()

            from execution.execution_engine import ExecutionEngine

            engine = ExecutionEngine(
                mode=ExecutionMode.DEMO,
                config=cfg,
                store=store,
                rest_client=rest,
            )
            executor = LiveExecutor(cfg, rest)
            engine._live = executor

            from execution.trade_manager import TradeManager

            tm = TradeManager(cfg, store, rest_client=rest)
            signal = TradeSignal(
                market="Japan 225",
                epic="IX.D.NIKKEI.IFM.IP",
                direction="BUY",
                raw_confidence=92.0,
                adjusted_confidence=92.0,
                setup_key="e2e|dry",
                quote=_quote(),
                snapshot={},
                notes="e2e dry run",
            )
            settings = {"size": 0.5, "stop": 45.0, "limit": 90.0}

            with patch.object(executor, "_order_worker"):
                result = executor.execute(
                    signal, settings, tm, MagicMock(), mode=ExecutionMode.DEMO
                )

            self.assertTrue(result.success)
            rest.place_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
