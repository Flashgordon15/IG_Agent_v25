"""
End-to-end execution pipeline tests (mocked broker).

Proves: all orchestrator gates pass -> execution process_tick -> execute_trade.
Does NOT place real IG orders (safe for CI).

For live DEMO routing validation (no order), run:
  PYTHONPATH=src python3 scripts/e2e_execution_probe.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.adaptive_engine import AdaptiveEngine
from execution.order_validator import ValidationResult
from execution.trading_loop import TickOutcome
from execution.trading_loop import TradingLoop as ExecutionTickLoop
from execution.types import ExecutionMode, ExecutionResult, TradeSignal
from runtime.market_orchestrator import MarketOrchestrator, attach_snapshot_handlers
from signals.signal_engine import SignalResult
from system.config_loader import ConfigLoader
from trading.environment_scorer import (
    FACTOR_TREND_MAX,
    SAFE_DEFAULT_SCORE,
    EnvironmentScorer,
    _apply_session_style_weights,
    _session_style_utc,
)
from trading.points_engine import PointsEngine, set_points_state_path_for_tests
from trading.trading_loop import SOFT_BLOCK_NOT_IN_TOP_3
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


def _assert_no_env_scorer_fallback(
    testcase: unittest.TestCase,
    scorer: Any,
    *,
    market: str = "",
    quote: Quote | None = None,
) -> None:
    """Fail fast when environment scorer drops into exception fallback (flat 55 path)."""
    if isinstance(scorer, MagicMock):
        last = scorer.last_score()
    else:
        if market:
            scorer.score(market, quote=quote)
        last = scorer.last_score()
    testcase.assertFalse(
        bool(getattr(last, "fallback_active", False)),
        "environment_scorer fallback_active must be False (exception fallback detected)",
    )
    if (
        hasattr(scorer, "_compute_factors")
        and market
        and not isinstance(scorer, MagicMock)
    ):
        _, meta = scorer._compute_factors(market, quote=quote)
        testcase.assertFalse(
            bool(meta.get("fallback_active")),
            "environment_scorer meta must not flag fallback_active",
        )
        testcase.assertNotIn(
            "env_scorer_fallback",
            meta,
            "environment_scorer meta must not contain env_scorer_fallback key",
        )
    factors = scorer.get_factors() if hasattr(scorer, "get_factors") else {}
    if isinstance(factors, dict):
        testcase.assertFalse(
            bool(factors.get("fallback_active")),
            "environment_scorer factors must not flag fallback_active",
        )
        testcase.assertNotIn(
            "env_scorer_fallback",
            factors,
            "environment_scorer factors must not contain env_scorer_fallback key",
        )


def _make_engine_with_bars(n_5m: int = 25) -> MagicMock:
    rows = []
    for i in range(n_5m):
        rows.append(
            {
                "time": datetime(2026, 6, 10, 10, 0) + timedelta(minutes=i * 5),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "price": 100.5,
                "bid": 100.0,
                "offer": 100.5,
                "spread": 0.5,
                "fast_ema": 101.0,
                "slow_ema": 99.0,
                "rsi": 55.0,
                "atr": 10.0,
            }
        )
    df = pd.DataFrame(rows)
    c5 = df.copy()
    c15 = df.iloc[: max(3, len(df) // 3)].copy()
    engine = MagicMock()
    engine.quote_df.return_value = df
    engine.candles.side_effect = lambda _df, minutes: c5 if minutes == 5 else c15
    engine.add_indicators.side_effect = lambda frame: frame
    cfg = MagicMock()
    cfg.max_spread_points = 35.0
    engine.config = cfg
    return engine


def _make_orchestrator(**overrides) -> OrchestratorLoop:
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
    config.stop_distance_points = 45.0
    config.trade_size = 1.0
    config.currency_code = "GBP"
    config.max_daily_loss_gbp = 200.0
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
    env.last_score.return_value = SimpleNamespace(
        total=55.0,
        fallback_active=False,
        session_style="WESTERN_MOMENTUM",
        regime="Marginal",
        factors={},
        capped_cold_start=False,
        capped_gap_open=False,
        gate_passes=True,
    )
    env.get_factors.return_value = {
        "atr": 100.0,
        "trend": 12.5,
        "session": 10.0,
        "spread": 12.5,
        "fallback_active": False,
    }

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
    @patch(
        "system.market_watch.japan225_session.japan225_strategy_paused",
        return_value=(False, ""),
    )
    def test_all_gates_pass_then_process_tick(self, _j225: MagicMock) -> None:
        loop = _make_orchestrator()
        ctx = loop.run_once()
        assert ctx is not None
        self.assertTrue(ctx.all_passed)
        self.assertEqual(len(ctx.gates), 11)
        self.assertIn("calendar_ok", [g.name for g in ctx.gates])
        self.assertIn("ml_veto", [g.name for g in ctx.gates])
        self.assertTrue(all(g.passed for g in ctx.gates))
        loop._execution_loop.process_tick.assert_called_once()
        _assert_no_env_scorer_fallback(self, loop._env)
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
            with patch(
                "execution.live_executor.epic_has_pending_open", return_value=False
            ):
                outcome = tick_loop.process_tick(
                    "Japan 225",
                    "IX.D.NIKKEI.IFM.IP",
                    _quote(),
                    gate_execution_params={
                        "actual_size": 1.0,
                        "stop_points": 45.0,
                        "limit_points": 90.0,
                    },
                )

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
        exec_engine.validate_only.return_value = ValidationResult(
            allowed=True, reasons=[], checks={}
        )
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


def _roadmap_env_scorer(fitness_total: float) -> MagicMock:
    env = MagicMock()
    trend = max(0.01, float(fitness_total) * 0.25)
    env._last = SimpleNamespace(total=float(fitness_total))
    env.get_factors.return_value = {
        "trend": trend,
        "spread": 15.0,
        "atr": 20.0,
        "session": 10.0,
    }
    env.last_score.return_value = env._last
    env.score.return_value = float(fitness_total)
    return env


def _roadmap_loop(epic: str, fitness_total: float, *, market: str) -> MagicMock:
    loop = MagicMock()
    loop._epic = epic
    loop._market = market
    loop._env = _roadmap_env_scorer(fitness_total)
    loop._publish_snapshots = False
    loop._on_snapshot = None
    return loop


def _build_four_market_orchestrator() -> MarketOrchestrator:
    """Four dummy instruments with descending fitness (DAX is choppy / lowest)."""
    cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
    epics = {
        "IX.D.NASDAQ.CASH.IP": ("IXIC", 88.0),
        "CS.D.CFPGOLD.CFP.IP": ("Spot Gold", 72.0),
        "CS.D.EURUSD.CFD.IP": ("EUR/USD", 61.0),
        "IX.D.DAX.IG.IP": ("DAX", 34.0),
    }
    loops = [
        _roadmap_loop(epic, score, market=label)
        for epic, (label, score) in epics.items()
    ]
    enabled = list(epics.keys())
    meta = {
        epic: {"name": label, "instrument_id": label.lower().replace(" ", "_")}
        for epic, (label, _) in epics.items()
    }
    return MarketOrchestrator(
        cfg,
        loops,
        primary_epic=enabled[0],
        enabled_epics=enabled,
        instrument_meta=meta,
    )


class TestRoadmapE2EIntegration(unittest.TestCase):
    """£1,000/Day Roadmap — orchestrator rotation, session weights, sizing, R:R."""

    def setUp(self) -> None:
        from runtime import market_orchestrator as mo

        self._orch_ref_backup = mo._ORCHESTRATOR_REF

    def tearDown(self) -> None:
        from runtime import market_orchestrator as mo

        mo._ORCHESTRATOR_REF = self._orch_ref_backup
        set_points_state_path_for_tests(None)

    @patch.object(
        MarketOrchestrator,
        "_strategy_session_eligible",
        return_value=True,
    )
    @patch("system.gate_relaxation.rotation_filter_bypassed", return_value=False)
    @patch("runtime.market_orchestrator.publish_tick")
    def test_multi_market_hub_ingress_ranks_active_epics_top_three(
        self, _publish: MagicMock, _rotation_bypass: MagicMock, _session: MagicMock
    ) -> None:
        orch = _build_four_market_orchestrator()
        attach_snapshot_handlers(orch)

        hub_ticks = [
            ("IX.D.NASDAQ.CASH.IP", "IXIC", 18000.0, 18000.5, 88.0),
            ("CS.D.CFPGOLD.CFP.IP", "Spot Gold", 2350.0, 2350.3, 72.0),
            ("CS.D.EURUSD.CFD.IP", "EUR/USD", 1.0850, 1.0852, 61.0),
            ("IX.D.DAX.IG.IP", "DAX", 18200.0, 18202.0, 34.0),
        ]
        for epic, market, bid, offer, fitness in hub_ticks:
            loop = next(lo for lo in orch.loops if lo._epic == epic)
            loop._env._last.total = fitness
            loop._env.get_factors.return_value = {
                "trend": max(0.01, float(fitness) * 0.25),
                "spread": 15.0,
                "atr": 20.0,
                "session": 10.0,
            }
            orch.on_market_snapshot(
                {
                    "epic": epic,
                    "market": market,
                    "bid": bid,
                    "offer": offer,
                    "spread": round(offer - bid, 4),
                    "signal": {"fitness": fitness},
                }
            )

        active = orch.get_active_epics()
        self.assertEqual(len(active), 3)
        self.assertNotIn("IX.D.DAX.IG.IP", active)
        self.assertEqual(
            active,
            [
                "IX.D.NASDAQ.CASH.IP",
                "CS.D.CFPGOLD.CFP.IP",
                "CS.D.EURUSD.CFD.IP",
            ],
        )

    @patch.object(
        MarketOrchestrator,
        "_strategy_session_eligible",
        return_value=True,
    )
    @patch("system.gate_relaxation.demo_soak_enabled", return_value=False)
    @patch(
        "system.gate_relaxation.rotation_filter_bypassed",
        return_value=False,
    )
    @patch(
        "system.market_watch.japan225_session.japan225_strategy_paused",
        return_value=(False, ""),
    )
    def test_choppy_asset_outside_top_three_soft_blocked(
        self,
        _j225: MagicMock,
        _rotation_bypass: MagicMock,
        _soak: MagicMock,
        _session: MagicMock,
    ) -> None:
        orch = _build_four_market_orchestrator()
        attach_snapshot_handlers(orch)
        for epic, fitness in (
            ("IX.D.NASDAQ.CASH.IP", 88.0),
            ("CS.D.CFPGOLD.CFP.IP", 72.0),
            ("CS.D.EURUSD.CFD.IP", 61.0),
            ("IX.D.DAX.IG.IP", 34.0),
        ):
            loop = next(lo for lo in orch.loops if lo._epic == epic)
            loop._env._last.total = fitness
        orch.refresh_active_epics()

        loop = _make_orchestrator(
            epic="IX.D.DAX.IG.IP",
            market="DAX",
        )
        loop._rotation_grace_remaining = 0
        gates = loop._evaluate_gates_core(_quote())
        self.assertFalse(any(g.passed for g in gates))
        self.assertTrue(
            all(g.detail == SOFT_BLOCK_NOT_IN_TOP_3 for g in gates),
            [g.detail for g in gates],
        )

    def test_session_style_utc_asian_and_western_weights(self) -> None:
        asian_now = datetime(2026, 6, 10, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(_session_style_utc(asian_now), "ASIAN_RANGE")
        base = {"atr": 20.0, "trend": 20.0, "session": 10.0, "spread": 15.0}
        asian = _apply_session_style_weights(dict(base), "ASIAN_RANGE")
        self.assertAlmostEqual(asian["trend"], 10.0)
        self.assertAlmostEqual(asian["trend"], base["trend"] * 0.5)

        western_now = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(_session_style_utc(western_now), "WESTERN_MOMENTUM")
        weak = _apply_session_style_weights(
            {"atr": 20.0, "trend": 4.0, "session": 10.0, "spread": 15.0},
            "WESTERN_MOMENTUM",
        )
        self.assertAlmostEqual(weak["trend"], min(FACTOR_TREND_MAX, 4.0 * 2.5))
        self.assertLess(weak["trend"], FACTOR_TREND_MAX * 0.5)
        self.assertAlmostEqual(weak["atr"], 20.0 * 0.4)
        self.assertAlmostEqual(weak["session"], 10.0 * 0.4)
        self.assertAlmostEqual(weak["spread"], 15.0 * 0.4)

        strong = _apply_session_style_weights(dict(base), "WESTERN_MOMENTUM")
        self.assertAlmostEqual(strong["trend"], FACTOR_TREND_MAX)
        self.assertAlmostEqual(strong["atr"], base["atr"])

        engine = _make_engine_with_bars(25)
        scorer = EnvironmentScorer(engine, config=engine.config, normal_spread=10.0)
        quote = Quote(datetime(2026, 6, 10, 14, 0), 100.0, 100.5)
        _assert_no_env_scorer_fallback(self, scorer, market="US Tech 100", quote=quote)

    def test_environment_scorer_exception_fallback_flags_active(self) -> None:
        engine = _make_engine_with_bars(25)
        scorer = EnvironmentScorer(engine, config=engine.config, normal_spread=10.0)
        quote = Quote(datetime(2026, 6, 10, 14, 0), 100.0, 100.5)
        with patch.object(
            scorer,
            "_compute_factors",
            side_effect=NameError("session_style"),
        ):
            score = scorer.score("US Tech 100", quote=quote)
        self.assertEqual(score, SAFE_DEFAULT_SCORE)
        self.assertTrue(scorer.last_score().fallback_active)

    def test_roadmap_cumulative_sizing_boost_and_clamp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        set_points_state_path_for_tests(Path(tmp.name) / "points.json")
        engine = PointsEngine(state_path=Path(tmp.name) / "points.json")
        engine._cumulative = 16.0
        self.assertAlmostEqual(engine._finalize_size_multiplier(0.5), 1.25)

        cfg = MagicMock()
        cfg.trade_size = 1.0
        cfg.adaptive_min_trade_size = 0.01
        cfg.adaptive_max_trade_size = 0.75
        with patch("system.config_loader.get_config", return_value=cfg):
            self.assertAlmostEqual(engine._finalize_size_multiplier(0.5), 0.75)

    def test_asymmetric_rr_floor_blocks_below_three_to_one(self) -> None:
        cfg = MagicMock()
        cfg.adaptive_execution_enabled = True
        cfg.adaptive_min_adjusted_confidence = 0.0
        cfg.adaptive_max_entry_spread = 9999.0
        cfg.adaptive_min_net_profit_pts = 0.0
        cfg.adaptive_block_bad_setups = False

        adaptive = AdaptiveEngine(cfg)
        with patch.object(
            adaptive,
            "settings",
            return_value={"risk": 40.0, "limit": 100.0},
        ):
            with patch("execution.adaptive_engine.get_config", return_value=cfg):
                blocked, reason = adaptive.should_block("test|roadmap", 92.0, {})

        self.assertTrue(blocked)
        self.assertEqual(reason, "REJECTED_ASYMMETRIC_RR_FLOOR_GATED")

    def test_atr_cap_skipped_when_below_rr_floor(self) -> None:
        cfg = MagicMock()
        cfg.adaptive_execution_enabled = True
        cfg.adaptive_atr_risk_enabled = True
        cfg.dynamic_stop_floor_enabled = True
        cfg.dynamic_stop_floor_min = 5.0
        cfg.adaptive_min_risk_points = 5.0
        cfg.adaptive_max_risk_points = 40.0
        cfg.atr_multiplier = 1.0
        cfg.default_stop_distance_points = 10.0
        cfg.reward_multiple = 3.0
        cfg.adaptive_high_confidence = 95.0
        cfg.adaptive_high_confidence_reward_multiple = 3.0
        cfg.adaptive_min_setup_trades = 99
        cfg.adaptive_good_winrate_threshold = 0.6
        cfg.adaptive_bad_winrate_threshold = 0.4
        cfg.adaptive_good_setup_reward_multiple = 2.4
        cfg.adaptive_bad_setup_reward_multiple = 1.4
        cfg.adaptive_good_setup_multiplier = 1.0
        cfg.adaptive_bad_setup_multiplier = 1.0
        cfg.adaptive_min_trade_size = 0.01
        cfg.adaptive_max_trade_size = 10.0
        cfg.adaptive_max_limit_atr_multiple = 4.0
        cfg.trade_size = 1.0
        cfg.get = MagicMock(return_value=True)

        adaptive = AdaptiveEngine(cfg)
        snapshot = {"last": {"atr": 6.2, "spread": 0.3}}
        with patch("execution.adaptive_engine.get_config", return_value=cfg):
            settings = adaptive.settings(
                "SELL|bear|us_afternoon|atr0-30|rsilow|volhigh", 81.0, snapshot
            )

        risk = float(settings["risk"])
        limit = float(settings["limit"])
        self.assertGreaterEqual(limit / risk, 3.0 - 1e-6)
        self.assertIn("skipped", settings["notes"])


if __name__ == "__main__":
    unittest.main()
