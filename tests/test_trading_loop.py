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
from trading.trading_loop import (
    BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER,
    STAGE1_GBP_RISK_CAP,
    GateResult,
    TradingLoop,
    signal_gate_explanation,
)


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
        snapshot={"atr": 50.0},
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
    # Ensure persisted rate-limit state from production runs doesn't leak into tests.
    try:
        from system.rate_limit_manager import get_rate_limit_manager

        get_rate_limit_manager().reset_for_tests()
    except Exception:
        pass
    config = MagicMock()
    config.refresh_seconds = 0.05
    config.max_spread_points = 35.0
    config.max_positions_per_epic = 1
    config.max_open_positions = 3
    config.stop_distance_points = 40.0
    config.trade_size = 1.0
    config.adaptive_min_trade_size = 0.5
    config.adaptive_max_trade_size = 5.0
    config.currency_code = "GBP"
    config.max_daily_loss_gbp = 200.0
    config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "ig_point_value_gbp": 1.0,
            "risk_cap_gbp": None,
            "enforce_top3_rotation_filter": False,
            "spread_to_atr_circuit_breaker_max": 0.30,
        }.get(key, default)
    )

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
    env.get_factors.return_value = {"atr": 50.0}

    points = MagicMock()
    points.get_state.return_value = "HEALTHY"
    points.is_session_paused.return_value = False
    points.is_day_stopped.return_value = False
    points.get_threshold.return_value = 80.0
    points.get_size_multiplier.return_value = 1.0
    points.trade_confidence_threshold.return_value = 80.0
    points.snapshot.return_value = MagicMock(
        cumulative=0.0,
        session_score=0.0,
        last_trade_score=0.0,
    )

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = _wait_signal()
    signal_engine.quote_df.return_value = None
    signal_engine.last_snapshot = {}

    exec_engine = MagicMock()
    exec_engine.trade_tracker.count_open_for_epic.return_value = 0
    exec_engine.trade_tracker.count_open_total.return_value = 0
    exec_engine.trade_tracker.snapshot.return_value = {"positions": []}
    exec_engine.update_positions = MagicMock()
    adaptive = MagicMock()
    adaptive.settings.side_effect = lambda *a, **k: {
        "risk": float(config.stop_distance_points)
    }
    exec_engine._adaptive = adaptive

    execution_loop = MagicMock()
    execution_loop.execution_engine = exec_engine
    execution_loop.process_tick = MagicMock(
        return_value=TickOutcome(
            quote=_quote(),
            signal=_buy_signal(92.0),
            trade_signal=MagicMock(),
            validation=MagicMock(allowed=True, reasons=[], checks={}),
            execution=MagicMock(success=True, action="SUBMITTED", rejection_reason=""),
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
        with patch(
            "system.learning_demo_policy.learning_demo_enabled", return_value=False
        ):
            ctx = loop.run_once()
        assert ctx is not None
        env_gate = next(g for g in ctx.gates if g.name == "environment_fitness")
        self.assertTrue(env_gate.passed)
        # SAFE_DEFAULT_SCORE == GATE_PASS_MIN == 55 — gate just passes, detail shows 55%
        self.assertIn("55%", env_gate.detail)
        self.assertNotIn("scorer unavailable", env_gate.detail)

    @patch(
        "system.market_watch.japan225_session.japan225_strategy_paused",
        return_value=(False, ""),
    )
    def test_process_tick_when_all_gates_pass(self, _j225: MagicMock) -> None:
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
        rest.get_cached_account_summary.return_value = {
            "balance": 10_000.0,
            "profit_loss": -12.5,
            "available": 9_500.0,
        }
        rest.maybe_refresh_account_summary.return_value = (
            rest.get_cached_account_summary.return_value
        )
        loop._execution_loop.execution_engine._rest_client = rest
        loop._store.recent_closed_trades.return_value = [
            {"result": "WIN"},
            {"result": "LOSS"},
            {"result": "WIN"},
        ]
        loop._store.sum_daily_pnl.return_value = -12.5
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


class RiskValidationGateTests(unittest.TestCase):
    def test_uses_ig_min_deal_size_for_actual_risk(self) -> None:
        loop = _make_loop()
        loop._epic = "CS.D.CFPGOLD.CFP.IP"
        loop._config.stop_distance_points = 10.0
        loop._config.trade_size = 10.0
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": 150,
            }.get(key, default)
        )
        loop._points.get_size_multiplier.return_value = 0.25
        rest = MagicMock()
        rest.fetch_market_constraints.return_value = {"min_deal_size": 10.0}
        loop._execution_loop.execution_engine._rest_client = rest

        with (
            patch("system.market_data_hub.get_market_data_hub") as hub_mock,
            patch("system.risk_bands.bands_enabled", return_value=False),
        ):
            hub_mock.return_value.normal_spread.return_value = 1.0
            gate = loop._gate_risk_validation(_quote())

        self.assertTrue(gate.passed)
        self.assertEqual(gate.value["effective_size"], 2.5)
        self.assertEqual(gate.value["actual_size"], 10.0)
        self.assertEqual(gate.value["ig_min_deal_size"], 10.0)
        self.assertEqual(gate.value["risk_gbp"], 100.0)
        self.assertEqual(gate.value["risk_cap_gbp"], 150)

    def test_per_instrument_risk_cap_override(self) -> None:
        loop = _make_loop()
        loop._config.stop_distance_points = 45.0
        loop._config.trade_size = 1.0
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": 50,
            }.get(key, default)
        )
        loop._points.get_size_multiplier.return_value = 1.0
        rest = MagicMock()
        rest.fetch_market_constraints.return_value = {"min_deal_size": 1.0}
        loop._execution_loop.execution_engine._rest_client = rest

        with patch("system.market_data_hub.get_market_data_hub") as hub_mock:
            hub_mock.return_value.normal_spread.return_value = 10.0
            gate = loop._gate_risk_validation(_quote())

        self.assertTrue(gate.passed)
        self.assertEqual(gate.value["risk_gbp"], 45.0)
        self.assertEqual(gate.value["risk_cap_gbp"], 50)

    def test_falls_back_to_stage1_cap_when_no_instrument_override(self) -> None:
        loop = _make_loop()
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": None,
            }.get(key, default)
        )
        rest = MagicMock()
        rest.fetch_market_constraints.return_value = {"min_deal_size": 1.0}
        loop._execution_loop.execution_engine._rest_client = rest

        with patch("system.market_data_hub.get_market_data_hub") as hub_mock:
            hub_mock.return_value.normal_spread.return_value = 10.0
            gate = loop._gate_risk_validation(_quote())

        self.assertEqual(gate.value["risk_cap_gbp"], STAGE1_GBP_RISK_CAP)


class DynamicMaxPerEpicTests(unittest.TestCase):
    """Unit tests for TradingLoop._dynamic_max_per_epic."""

    def _tracker(self, positions: list) -> MagicMock:
        t = MagicMock()
        t.snapshot.return_value = {"positions": positions}
        return t

    def _pos(self, epic: str, pnl_gbp: float, open_mins: float) -> dict:
        return {"epic": epic, "pnl_gbp": pnl_gbp, "open_mins": open_mins}

    def test_base_when_no_open_positions(self) -> None:
        loop = _make_loop()
        loop._epic = "IX.D.DOW.IFM.IP"
        loop._points.get_state.return_value = "HEALTHY"
        cap, reason = loop._dynamic_max_per_epic(2, 0, self._tracker([]))
        self.assertEqual(cap, 2)

    def test_base_when_points_not_healthy(self) -> None:
        loop = _make_loop()
        loop._epic = "IX.D.DOW.IFM.IP"
        loop._points.get_state.return_value = "CAUTION"
        pos = self._pos("IX.D.DOW.IFM.IP", 10.0, 25.0)
        cap, reason = loop._dynamic_max_per_epic(2, 1, self._tracker([pos]))
        self.assertEqual(cap, 2)
        self.assertIn("CAUTION", reason)

    def test_base_when_position_unprofitable(self) -> None:
        loop = _make_loop()
        loop._epic = "IX.D.DOW.IFM.IP"
        loop._points.get_state.return_value = "HEALTHY"
        pos = self._pos("IX.D.DOW.IFM.IP", -5.0, 25.0)
        cap, _ = loop._dynamic_max_per_epic(2, 1, self._tracker([pos]))
        self.assertEqual(cap, 2)

    def test_base_when_one_of_two_unprofitable(self) -> None:
        loop = _make_loop()
        loop._epic = "IX.D.DOW.IFM.IP"
        loop._points.get_state.return_value = "HEALTHY"
        positions = [
            self._pos("IX.D.DOW.IFM.IP", 10.0, 25.0),
            self._pos("IX.D.DOW.IFM.IP", -2.0, 10.0),
        ]
        cap, _ = loop._dynamic_max_per_epic(2, 2, self._tracker(positions))
        self.assertEqual(cap, 2)

    def test_plus1_when_all_profitable_young(self) -> None:
        """All profitable but oldest < 20 min → +1 only."""
        loop = _make_loop()
        loop._epic = "CS.D.CFPGOLD.CFP.IP"
        loop._points.get_state.return_value = "HEALTHY"
        pos = self._pos("CS.D.CFPGOLD.CFP.IP", 8.0, 10.0)
        cap, reason = loop._dynamic_max_per_epic(2, 1, self._tracker([pos]))
        self.assertEqual(cap, 3)
        self.assertIn("profitable", reason)

    def test_plus2_when_all_profitable_mature(self) -> None:
        """All profitable and oldest >= 20 min → +2."""
        loop = _make_loop()
        loop._epic = "CS.D.CFPGOLD.CFP.IP"
        loop._points.get_state.return_value = "HEALTHY"
        positions = [
            self._pos("CS.D.CFPGOLD.CFP.IP", 15.0, 25.0),
            self._pos("CS.D.CFPGOLD.CFP.IP", 5.0, 10.0),
        ]
        cap, reason = loop._dynamic_max_per_epic(2, 2, self._tracker(positions))
        self.assertEqual(cap, 4)
        self.assertIn("25m", reason)

    def test_filters_other_epics(self) -> None:
        """Positions on a different epic don't count toward profitable check."""
        loop = _make_loop()
        loop._epic = "IX.D.DOW.IFM.IP"
        loop._points.get_state.return_value = "HEALTHY"
        positions = [
            self._pos("CS.D.CFPGOLD.CFP.IP", 50.0, 30.0),  # different epic
        ]
        cap, _ = loop._dynamic_max_per_epic(2, 1, self._tracker(positions))
        self.assertEqual(cap, 2)

    def test_gate_value_exposes_dynamic_fields(self) -> None:
        """risk_validation gate value includes max_per_epic_base and unlock reason."""
        loop = _make_loop()
        loop._epic = "CS.D.CFPGOLD.CFP.IP"
        loop._config.max_positions_per_epic = 2
        loop._config.stop_distance_points = 10.0
        loop._config.trade_size = 1.0
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": 500,
            }.get(key, default)
        )
        loop._points.get_state.return_value = "HEALTHY"
        loop._points.get_size_multiplier.return_value = 1.0
        # One profitable, mature position → should unlock to 3
        loop._execution_loop.execution_engine.trade_tracker.count_open_for_epic.return_value = 1
        loop._execution_loop.execution_engine.trade_tracker.count_open_total.return_value = 1
        loop._execution_loop.execution_engine.trade_tracker.snapshot.return_value = {
            "positions": [
                {"epic": "CS.D.CFPGOLD.CFP.IP", "pnl_gbp": 12.0, "open_mins": 25.0}
            ]
        }
        rest = MagicMock()
        rest.fetch_market_constraints.return_value = {"min_deal_size": 1.0}
        loop._execution_loop.execution_engine._rest_client = rest

        with patch("system.market_data_hub.get_market_data_hub") as hub_mock:
            hub_mock.return_value.normal_spread.return_value = 0.5
            gate = loop._gate_risk_validation(_quote())

        self.assertEqual(gate.value["max_per_epic_base"], 2)
        self.assertEqual(gate.value["max_per_epic"], 4)
        self.assertIn("profitable", gate.value["dynamic_unlock_reason"])
        self.assertTrue(gate.passed)


class SpreadAtrCircuitBreakerTests(unittest.TestCase):
    def test_uses_signal_atr_points_not_fitness_factor(self) -> None:
        """Fitness factor scores (0–30) must not drive spread/ATR circuit math."""
        loop = _make_loop()
        loop._env.get_factors.return_value = {"atr": 2.2}
        loop._signal_engine.evaluate.return_value = SignalResult(
            signal="WAIT",
            raw_confidence=0.0,
            adjusted_confidence=0.0,
            learning_delta=0.0,
            setup_key="",
            notes="wait",
            snapshot={"last": {"atr": 100.0}},
        )
        quote = Quote(datetime(2026, 5, 27, 14, 0), 100.0, 102.4)

        with patch("system.market_data_hub.get_market_data_hub") as hub_mock:
            hub_mock.return_value.verify_liquidity_shield_delta.return_value = (
                True,
                1.0,
            )
            gates = loop._evaluate_gates_core(quote)

        self.assertGreater(len(gates), 1)
        risk = next(g for g in gates if g.name == "risk_validation")
        self.assertNotEqual(risk.detail, BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER)


if __name__ == "__main__":
    unittest.main()
