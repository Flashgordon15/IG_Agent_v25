"""
High-speed QMM unified pipeline integration test (in-memory, <5s).

Verifies end-to-end wiring:
  Stage 1 — MarketOrchestrator quote ingress + extract_live_state_vector
  Stage 2 — 12 compliance gates (Gate 10 vol floor, Gate 11 fractional sizing)
  Stage 3 — PortfolioEnvelope atomic allocation lock
  Stage 4 — OrderConfirmWorker async SUBMITTED handoff
  Stage 5 — Worker failure shield + release_allocation rollback
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from api.snapshot import GATE_NAMES
from data.models import Quote
from execution.cooldown_tracker import CooldownTracker
from execution.entry_inflight import reset_entry_inflight_state_for_tests
from execution.live_executor import LiveExecutor
from execution.pending_order_reconcile import reset_pending_state_for_tests
from execution.trade_manager import TradeManager
from execution.types import ExecutionMode, ExecutionResult, TradeSignal
from ml.interim_scorer import extract_live_state_vector
from runtime.market_orchestrator import MarketOrchestrator
from signals.signal_engine import SignalResult
from system.config import Config
from system.portfolio_envelope import (
    can_allocate,
    release_allocation,
    reset_portfolio_envelope_for_tests,
    snapshot as portfolio_snapshot,
)
from trading.trading_loop import TradingLoop

_FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "qmm_unified_pipeline_config.json"


def _localized_qmm_config() -> Config:
    """Isolated overlay — never reads production config_v29.json paths."""
    if _FIXTURE_CONFIG.is_file():
        data = json.loads(_FIXTURE_CONFIG.read_text(encoding="utf-8"))
    else:
        data = {
            "operating_mode": "DEMO",
            "dry_run": False,
            "allow_live_trading": True,
            "signal_threshold": 85,
            "trade_size": 1.0,
            "stop_distance_points": 40.0,
            "default_stop_distance_points": 40.0,
            "risk_points": 40,
            "max_spread_points": 35.0,
            "max_spread": 35.0,
            "max_positions_per_epic": 2,
            "max_open_positions": 5,
            "reward_multiple": 2.0,
            "adaptive_min_trade_size": 0.5,
            "adaptive_max_trade_size": 5.0,
            "enforce_top3_rotation_filter": False,
            "spread_to_atr_circuit_breaker_max": 10.0,
            "protective_learning": {
                "session_score_floor": -30.0,
                "signal_confidence_floor_min": 10.0,
                "high_vol_atr_multiplier": 0.25,
                "volatility_threshold_relax_strength": 0.95,
            },
            "qmm_framework": {
                "rotation_refresh_seconds": 60,
                "scalp_atr_multiplier_max": 0.35,
                "trend_atr_multiplier_min": 0.85,
            },
        }
    return Config(_data=data)


def _fresh_quote() -> Quote:
    return Quote(datetime.now(timezone.utc), 39000.0, 39007.0)


def _buy_signal(conf: float = 88.0, *, atr: float = 50.0) -> SignalResult:
    return SignalResult(
        signal="BUY",
        raw_confidence=conf,
        adjusted_confidence=conf,
        learning_delta=0.0,
        setup_key="qmm|unified|e2e",
        notes="qmm unified pipeline",
        snapshot={"last": {"atr": atr, "rsi": 62}, "raw_confidence": conf},
    )


def _make_trading_loop(**overrides) -> TradingLoop:
    cfg = _localized_qmm_config()
    session = MagicMock()
    session.is_session_open.return_value = True
    session.is_cold_start.return_value = False
    session.check_gap_open.return_value = False
    session.bars_since_open.return_value = 12
    session.should_flatten.return_value = False
    session.should_run_flatten_attempt.return_value = False
    session.is_entry_blocked_near_session_end.return_value = (False, None)
    session.snapshot.return_value = MagicMock(phase="OPEN")
    session.on_tick = MagicMock()

    env = MagicMock()
    env.score.return_value = 62.0
    env.get_factors.return_value = {"atr": 50.0, "spread": 15.0, "trend": 12.0}

    points = MagicMock()
    points.get_state.return_value = "HEALTHY"
    points.is_session_paused.return_value = False
    points.is_day_stopped.return_value = False
    points.get_threshold.return_value = 80.0
    points.trade_confidence_threshold.return_value = 85.0
    points.min_size_confidence_threshold.return_value = 80.0
    points.get_size_multiplier.return_value = 1.0
    points.snapshot.return_value = MagicMock(
        cumulative=0.0,
        session_score=5.0,
        last_trade_score=0.0,
        nominal_state="HEALTHY",
    )

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = _buy_signal()
    signal_engine.quote_df.return_value = None
    signal_engine.last_snapshot = {}

    exec_engine = MagicMock()
    exec_engine.trade_tracker.count_open_for_epic.return_value = 0
    exec_engine.trade_tracker.count_open_total.return_value = 0
    exec_engine.trade_tracker.snapshot.return_value = {"positions": []}
    exec_engine.update_positions = MagicMock()
    adaptive = MagicMock()
    adaptive.settings.side_effect = lambda *a, **k: {"risk": 40.0}
    exec_engine._adaptive = adaptive

    execution_loop = MagicMock()
    execution_loop.execution_engine = exec_engine

    store = MagicMock()
    store.sum_daily_pnl.return_value = 0.0

    kwargs = {
        "config": cfg,
        "market": "Japan 225",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "session_manager": session,
        "environment_scorer": env,
        "points_engine": points,
        "signal_engine": signal_engine,
        "execution_loop": execution_loop,
        "quote_source": _fresh_quote,
        "learning_store": store,
        "tick_interval_sec": 0.05,
    }
    kwargs.update(overrides)
    loop = TradingLoop(**kwargs)
    loop._gate_signal_cache = None
    return loop


def _trade_signal() -> TradeSignal:
    q = _fresh_quote()
    return TradeSignal(
        market="Japan 225",
        epic="IX.D.NIKKEI.IFM.IP",
        direction="BUY",
        raw_confidence=92.0,
        adjusted_confidence=92.0,
        setup_key="qmm|worker",
        quote=q,
        notes="qmm unified worker",
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


class QmmUnifiedPipelineTests(unittest.TestCase):
    """Five-stage in-memory verification of the QMM hot path."""

    def setUp(self) -> None:
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        reset_portfolio_envelope_for_tests()
        try:
            from execution.correlation_guard import reset_correlation_guard_for_tests

            reset_correlation_guard_for_tests()
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
        reset_portfolio_envelope_for_tests()

    def test_unified_pipeline_five_stages(self) -> None:
        t0 = time.perf_counter()

        # ------------------------------------------------------------------
        # Stage 1 — Asset selector ingress + in-RAM feature vector extract
        # ------------------------------------------------------------------
        loop_a = _make_trading_loop()
        loop_b = _make_trading_loop(
            market="EUR/USD",
            epic="CS.D.EURUSD.CFD.IP",
        )
        orch = MarketOrchestrator(
            _localized_qmm_config(),
            [loop_a, loop_b],
            primary_epic=loop_a._epic,
            enabled_epics=[loop_a._epic, loop_b._epic],
            instrument_meta={
                loop_a._epic: {"name": "Japan 225", "instrument_id": "japan_225"},
                loop_b._epic: {"name": "EUR/USD", "instrument_id": "eur_usd"},
            },
        )
        quote_packet = {
            "epic": loop_a._epic,
            "market": loop_a._market,
            "bid": 39000.0,
            "offer": 39007.0,
            "spread": 7.0,
        }
        with (
            patch.object(orch, "_apply_feed_circuit_breakers", return_value=set()),
            patch.object(orch, "_loop_providing_live_data", return_value=True),
            patch.object(orch, "_strategy_session_eligible", return_value=True),
            patch("trading.qmm_asset_selector._hub_atr_delta", return_value=0.18),
            patch("runtime.market_orchestrator.publish_tick"),
        ):
            orch.on_market_snapshot(quote_packet)
            active = orch.get_active_epics()
        self.assertIn(loop_a._epic, active)

        quote = _fresh_quote()
        vector = extract_live_state_vector(
            loop_a._market,
            quote,
            {
                "session_score": 5.0,
                "nominal_state": "HEALTHY",
                "atr_multiplier": 1.25,
            },
        )
        self.assertIsInstance(vector, dict)
        for key in ("spread", "quote_age_s", "atr_multiplier", "session_score"):
            self.assertIn(key, vector)
        self.assertIsInstance(vector["spread"], (int, float))
        self.assertIsInstance(vector["quote_age_s"], (int, float))

        # ------------------------------------------------------------------
        # Stage 2 — 12 gates: Gate 10 vol floor + Gate 11 fractional sizing
        # ------------------------------------------------------------------
        loop = _make_trading_loop()
        loop._last_ml_prob = 0.54
        loop._last_sig_direction = "BUY"
        loop._signal_engine.evaluate.return_value = _buy_signal(88.0, atr=50.0)

        ml_settings = {
            "enabled": True,
            "min_probability": 0.58,
            "marginal_prob_band": 0.06,
            "min_sizing_multiplier": 0.25,
            "use_s4_models": False,
            "per_epic": {},
        }
        hub = MagicMock()
        hub.normal_spread.return_value = 10.0
        hub.verify_liquidity_shield_delta.return_value = (True, 1.0)
        hub.is_in_maintenance.return_value = False

        with (
            patch(
                "system.market_watch.japan225_session.japan225_strategy_paused",
                return_value=(False, ""),
            ),
            patch("system.gate_relaxation.rotation_filter_bypassed", return_value=True),
            patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=False),
            patch("system.market_data_hub.get_market_data_hub", return_value=hub),
            patch("system.gate_relaxation.soak_ml_veto_bypassed", return_value=False),
            patch("system.v26_config.ml_veto_settings", return_value=ml_settings),
            patch("system.v26_config.epic_ml_veto_enabled", return_value=True),
            patch("system.v26_config.epic_min_probability", return_value=0.58),
            patch("system.config_loader.get_config", return_value=loop._config),
        ):
            low_atr_sig = _buy_signal(88.0, atr=5.0)
            loop._signal_engine.evaluate.return_value = low_atr_sig
            loop._gate_signal_cache = None
            loop._reset_tick_memo()
            low_gate = loop._gate_signal_confidence(quote)
            low_threshold = float(low_gate.value["threshold"])

            loop._signal_engine.evaluate.return_value = _buy_signal(88.0, atr=50.0)
            loop._gate_signal_cache = None
            loop._reset_tick_memo()
            high_gate = loop._gate_signal_confidence(quote)
            high_threshold = float(high_gate.value["threshold"])

            loop._signal_engine.evaluate.return_value = _buy_signal(88.0, atr=50.0)
            loop._gate_signal_cache = None

            real_signal_confidence = loop._gate_signal_confidence

            def _signal_confidence_preserving_ml(quote_arg: Quote) -> object:
                result = real_signal_confidence(quote_arg)
                loop._last_ml_prob = 0.54
                return result

            loop._gate_signal_confidence = _signal_confidence_preserving_ml
            gates = loop._evaluate_gates(quote)

        self.assertLess(
            high_threshold,
            low_threshold,
            "Gate 10 should lower confidence floor in high-vol regime",
        )
        self.assertIn("live_state_vector", high_gate.value)

        gate_names = [g.name for g in gates]
        self.assertEqual(len(gates), len(GATE_NAMES))
        self.assertEqual(set(gate_names), set(GATE_NAMES))

        ml_gate = next(g for g in gates if g.name == "ml_veto")
        self.assertTrue(ml_gate.passed)
        self.assertIsInstance(ml_gate.value, dict)
        ml_mult = float(ml_gate.value["sizing_multiplier"])
        self.assertTrue(ml_gate.value.get("marginal_scaled"))
        self.assertGreater(ml_mult, 0.25)
        self.assertLess(ml_mult, 1.0)
        self.assertAlmostEqual(ml_mult, 0.5, delta=0.15)

        risk_gate = next(g for g in gates if g.name == "risk_validation")
        self.assertAlmostEqual(
            float(risk_gate.value["ml_sizing_multiplier"]),
            ml_mult,
            places=3,
        )

        # ------------------------------------------------------------------
        # Stage 3 — Atomic portfolio allocation lock
        # ------------------------------------------------------------------
        reset_portfolio_envelope_for_tests()
        with patch(
            "system.portfolio_envelope._envelope_config",
            return_value={
                "max_concurrent_risk_gbp": 50.0,
                "max_daily_risk_deployed_gbp": 500.0,
                "min_available_gbp": 0.0,
                "account_balance_gbp": 10000.0,
                "reserve_pct": 0.0,
            },
        ):
            results: list[bool] = []

            def _claim() -> None:
                ok, _ = can_allocate(40.0, reserve=True)
                results.append(ok)

            t1 = threading.Thread(target=_claim)
            t2 = threading.Thread(target=_claim)
            t1.start()
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertEqual(sum(results), 1)
            snap = portfolio_snapshot()
            self.assertAlmostEqual(snap["concurrent_risk_gbp"], 40.0)

        # ------------------------------------------------------------------
        # Stage 4 — Async worker: main thread gets SUBMITTED immediately
        # ------------------------------------------------------------------
        executor = _live_executor()
        trade_mgr = MagicMock(spec=TradeManager)
        cooldown = MagicMock(spec=CooldownTracker)
        client = executor._client
        client.place_market_order = MagicMock(
            side_effect=AssertionError("sync REST must not run on tick thread")
        )

        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as rate_mgr,
            patch(
                "execution.live_executor.japan225_daily_risk_paused",
                return_value=False,
            ),
            patch(
                "execution.correlation_guard.check_and_record",
                return_value=(True, ""),
            ),
            patch("system.portfolio_envelope.portfolio_gate_enabled", return_value=True),
            patch("system.portfolio_envelope.can_allocate", return_value=(True, "ok")),
            patch.object(
                executor,
                "_execute_order_blocking",
                return_value=ExecutionResult(
                    success=True,
                    action="EXECUTED",
                    deal_reference="REF-QMM",
                    deal_id="DEAL-QMM",
                ),
            ),
        ):
            rate_mgr.return_value.check_rest_allowed.return_value = None
            submit_t0 = time.perf_counter()
            result = executor.execute(
                _trade_signal(),
                _execution_params(),
                trade_mgr,
                cooldown,
                mode=ExecutionMode.DEMO,
            )
            submit_elapsed = time.perf_counter() - submit_t0
            self.assertEqual(result.action, "SUBMITTED")
            self.assertLess(submit_elapsed, 0.5)
            client.place_market_order.assert_not_called()
            executor.wait_pending_orders(timeout=3.0)

        # ------------------------------------------------------------------
        # Stage 5 — Worker failure shield + release_allocation rollback
        # ------------------------------------------------------------------
        reset_entry_inflight_state_for_tests()
        reset_pending_state_for_tests()
        executor.wait_pending_orders(timeout=1.0)
        reset_portfolio_envelope_for_tests()
        executor = _live_executor()
        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as rate_mgr,
            patch(
                "execution.live_executor.japan225_daily_risk_paused",
                return_value=False,
            ),
            patch(
                "execution.correlation_guard.check_and_record",
                return_value=(True, ""),
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
                    rejection_reason="IG size limit",
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
            executor.wait_pending_orders(timeout=3.0)

        mock_release.assert_called_once()
        self.assertAlmostEqual(float(mock_release.call_args[0][0]), 40.0)

        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0, f"unified pipeline took {elapsed:.2f}s (budget 5s)")


if __name__ == "__main__":
    unittest.main()
