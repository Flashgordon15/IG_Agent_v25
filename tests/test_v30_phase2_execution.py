"""
Phase 2 — Atomic execution gateway & warming circuit breaker verification.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.types import ExecutionMode, TradeSignal
from signals.signal_engine import SignalResult
from trading.trading_loop import TradingLoop

_WALL_ST = "IX.D.DOW.IFM.IP"
_CONFIDENCE_BREAKOUT = 92.0


def _quote(seq: int = 0) -> Quote:
    bid = 42000.0 + seq * 0.1
    return Quote(datetime(2026, 6, 19, 14, 30), bid, bid + 2.0)


class V30Phase2ExecutionTests(unittest.TestCase):
    def tearDown(self) -> None:
        from apex.microkernel import reset_microkernel_for_tests
        from apex.warmup_progress import reset_warmup_for_tests
        from execution.atomic_gateway import reset_gateway_for_tests
        from system.system_state import SystemState

        reset_gateway_for_tests()
        reset_microkernel_for_tests()
        reset_warmup_for_tests()
        SystemState.reset_singleton_for_tests()
        os.environ.pop("NODE_ENV", None)

    def test_warming_circuit_breaker_traps_high_confidence_signal(self) -> None:
        from apex.warmup_progress import reset_warmup_progress
        from execution.atomic_gateway import assert_execution_allowed, reset_gateway_for_tests
        from execution.execution_engine import ExecutionEngine
        from execution.live_executor import LiveExecutor
        from system.system_state import BootPhase, SystemState

        reset_gateway_for_tests()
        SystemState.reset_singleton_for_tests()
        state = SystemState.get()
        state.update_state(BootPhase.WARMING, 80, "Compiling Vector Arrays", ready=False)
        reset_warmup_progress(bars_target=256)

        self.assertEqual(assert_execution_allowed(), "HOLD: WARMING_CIRCUIT_BREAKER")

        log_lines: list[str] = []

        def _capture_log(msg: str, *args: object) -> None:
            log_lines.append(str(msg))

        loop = self._build_breakout_loop()
        with patch("system.config_loader.get_config", return_value=loop._config):
            with patch("trading.trading_loop.log_engine", side_effect=_capture_log):
                ctx = loop._run_tick_core()

        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertFalse(ctx.all_passed)
        self.assertEqual(ctx.wait_reason, "HOLD: WARMING_CIRCUIT_BREAKER")
        self.assertTrue(
            any("HOLD: WARMING_CIRCUIT_BREAKER" in line for line in log_lines),
            f"missing circuit breaker log; got {log_lines[-5:]}",
        )
        loop._execution_loop.process_tick.assert_not_called()

        cfg = MagicMock(
            allow_live_trading=True,
            account_type="DEMO",
            dry_run=False,
            cooldown_seconds=0,
        )
        engine = ExecutionEngine(
            mode=ExecutionMode.DEMO,
            config=cfg,
            store=MagicMock(),
            rest_client=MagicMock(),
        )
        signal = TradeSignal(
            market="Wall St",
            epic=_WALL_ST,
            direction="BUY",
            raw_confidence=_CONFIDENCE_BREAKOUT,
            adjusted_confidence=_CONFIDENCE_BREAKOUT,
            setup_key="BUY|phase2",
            quote=_quote(),
        )
        blocked = engine._execute_trade_body(signal)
        self.assertFalse(blocked.success)
        self.assertEqual(blocked.rejection_reason, "HOLD: WARMING_CIRCUIT_BREAKER")

        live_blocked = LiveExecutor(MagicMock(), cfg).execute(
            signal, {"size": 2.0}, MagicMock(), MagicMock(), mode=ExecutionMode.DEMO
        )
        self.assertFalse(live_blocked.success)
        self.assertEqual(live_blocked.rejection_reason, "HOLD: WARMING_CIRCUIT_BREAKER")

    def test_atomic_gateway_under_min_lot_and_integer_promotion(self) -> None:
        from apex.hardening import floor_contract_size
        from execution.atomic_gateway import (
            dispatch_atomic_market_order,
            promote_and_validate_lot,
            reset_gateway_for_tests,
            validate_risk_envelope,
        )
        from signals.signal_engine import SignalResult
        from trading.trading_loop import promote_high_confidence_signal

        reset_gateway_for_tests()
        sig = SignalResult(
            signal="WAIT",
            raw_confidence=88.0,
            adjusted_confidence=88.0,
            learning_delta=0.0,
            setup_key="BUY|test",
            notes="phase2",
            snapshot={"raw_signal": "BUY", "buy_score": 88.0},
        )
        promoted = promote_high_confidence_signal(sig, 45.0, raw_size=0.6)
        self.assertEqual(promoted.signal, "BUY")
        self.assertTrue(promoted.snapshot.get("under_min_lot"))
        self.assertEqual(promoted.snapshot.get("dispatch_size_int"), 0)

        _, size_int, hold = promote_and_validate_lot(sig, 45.0, 0.6)
        self.assertEqual(hold, "HOLD: UNDER_MIN_LOT")
        self.assertEqual(size_int, 0)

        size_ok, under = floor_contract_size(2.9)
        self.assertEqual(size_ok, 2)
        self.assertFalse(under)

        self.assertIsNone(validate_risk_envelope(risk_gbp=200.0, concurrent_gbp=400.0))
        block = validate_risk_envelope(risk_gbp=300.0, concurrent_gbp=500.0)
        self.assertIsNotNone(block)
        self.assertIn("PORTFOLIO_CEILING", block or "")

        client = MagicMock()
        client.place_market_order.return_value = {
            "dealReference": "REF-1",
            "status": "ACCEPTED",
        }
        result = dispatch_atomic_market_order(
            client,
            epic=_WALL_ST,
            direction="BUY",
            size=2.0,
            stop_distance=10.0,
            currency_code="GBP",
            trailing_distance_points=10.0,
        )
        self.assertEqual(result.get("dispatch_size_int"), 2)
        client.place_market_order.assert_called_once()
        call_kw = client.place_market_order.call_args.kwargs
        self.assertEqual(call_kw["size"], 2.0)

    def test_ig_radio_silence_blocks_passive_rest(self) -> None:
        from execution.atomic_gateway import (
            ig_radio_silence_blocks_rest,
            order_dispatch_lane,
            reset_gateway_for_tests,
        )
        from ig_api.rest_client import IGAPIError

        reset_gateway_for_tests()
        from system.system_state import BootPhase, get_system_state

        get_system_state().set_ready(label="ACTIVE")
        self.assertTrue(
            ig_radio_silence_blocks_rest("GET", "/gateway/deal/markets/CS.D.CFPGOLD.CFP.IP")
        )
        self.assertFalse(
            ig_radio_silence_blocks_rest("POST", "/gateway/deal/positions/otc")
        )
        with order_dispatch_lane():
            self.assertFalse(
                ig_radio_silence_blocks_rest("GET", "/gateway/deal/markets/CS.D.CFPGOLD.CFP.IP")
            )

        get_system_state().update_state(BootPhase.G2, 20, "Broker Handshake", ready=False)
        self.assertFalse(ig_radio_silence_blocks_rest("GET", "/accounts"))
        get_system_state().set_ready(label="ACTIVE")
        self.assertTrue(
            ig_radio_silence_blocks_rest("GET", "/gateway/deal/markets/CS.D.CFPGOLD.CFP.IP")
        )

        client = MagicMock()
        client._base = "https://demo-api.ig.com"
        client.timeout_seconds = 10.0
        client.max_retries = 1
        client._session = MagicMock()
        client._session_path_protected = MagicMock(return_value=False)
        client.proactive_refresh_if_needed = MagicMock()

        from ig_api.rest_client import IGRestClient

        real = IGRestClient.__new__(IGRestClient)
        real._base = "https://demo-api.ig.com"
        real.timeout_seconds = 10.0
        real.max_retries = 1
        real._session = MagicMock()
        real._session_path_protected = lambda path: False  # noqa: ARG005
        real.proactive_refresh_if_needed = MagicMock()

        with self.assertRaises(IGAPIError) as ctx:
            real.request("GET", "/gateway/deal/markets/CS.D.CFPGOLD.CFP.IP")
        self.assertIn("IG_RADIO_SILENCE", str(ctx.exception))

    def _build_breakout_loop(self) -> TradingLoop:
        from execution.trading_loop import TickOutcome

        config = MagicMock()
        config.allow_live_trading = True
        config.dry_run = False
        config.stop_distance_points = 10.0
        config.min_atr_points = 0.0
        config.adaptive_min_trade_size = 1.0

        session = MagicMock()
        session.is_session_open.return_value = True
        session.session_open_time = None
        session.on_tick = MagicMock()

        env = MagicMock()
        env.score.return_value = {"score": 88, "factors": {"atr": 55.0}}

        points = MagicMock()
        points.evaluate.return_value = MagicMock(passed=True, detail="ok")

        breakout = SignalResult(
            signal="BUY",
            raw_confidence=_CONFIDENCE_BREAKOUT,
            adjusted_confidence=_CONFIDENCE_BREAKOUT,
            learning_delta=0.0,
            setup_key="BUY|momentum_breakout",
            notes="phase2 breakout",
            snapshot={"atr": 55.0, "raw_confidence": _CONFIDENCE_BREAKOUT},
        )
        signal_engine = MagicMock()
        signal_engine.evaluate.return_value = breakout
        signal_engine.last_snapshot = {}

        exec_engine = MagicMock()
        exec_engine.trade_tracker.count_open_for_epic.return_value = 0
        exec_engine.trade_tracker.count_open_total.return_value = 0
        exec_engine.trade_tracker.snapshot.return_value = {"positions": []}
        exec_engine.update_positions = MagicMock()

        execution_loop = MagicMock()
        execution_loop.auto_trade = True
        execution_loop.execution_engine = exec_engine
        execution_loop.process_tick = MagicMock(
            return_value=TickOutcome(
                quote=_quote(1),
                signal=breakout,
                trade_signal=MagicMock(),
                validation=MagicMock(allowed=True, reasons=[], checks={}),
                execution=MagicMock(success=True, action="SUBMITTED", rejection_reason=""),
            )
        )

        return TradingLoop(
            config=config,
            market="Wall St",
            epic=_WALL_ST,
            session_manager=session,
            environment_scorer=env,
            points_engine=points,
            signal_engine=signal_engine,
            execution_loop=execution_loop,
            quote_source=lambda: _quote(99),
            learning_store=MagicMock(sum_daily_pnl=MagicMock(return_value=0.0)),
            tick_interval_sec=0.05,
        )


if __name__ == "__main__":
    unittest.main()
