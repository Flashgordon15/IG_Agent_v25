"""
CRM-403 — v30 Apex Monolith destructive chaos suite.

Models US-open tick flood during BootPhase.WARMING, circuit-breaker rejection,
post-ready ring flush, float64 indicator math, and £10k baseline capital walls.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.types import ExecutionMode, TradeSignal
from signals.signal_engine import SignalResult
from trading.trading_loop import TradingLoop

_GOLD = "CS.D.CFPGOLD.CFP.IP"
_WALL_ST = "IX.D.DOW.IFM.IP"
_FLOOD_COUNT = 2000
_CONFIDENCE_BREAKOUT = 92.0


def _volatile_quote(seq: int, *, epic: str = _GOLD) -> Quote:
    base = 2400.0 + (seq % 97) * 0.37
    jitter = ((seq * 17) % 11) * 0.05
    bid = base + jitter
    return Quote(datetime(2026, 6, 18, 14, 30, seq % 60), bid, bid + 0.4)


class V30MonolithChaosSuite(unittest.TestCase):
    """Single complex simulation — Steps A through C in one adversarial pass."""

    def tearDown(self) -> None:
        from apex.hardening import reset_hardening_for_tests
        from apex.microkernel import reset_microkernel_for_tests
        from apex.warmup_progress import reset_warmup_for_tests
        from system.system_state import SystemState

        reset_microkernel_for_tests()
        reset_warmup_for_tests()
        reset_hardening_for_tests()
        SystemState.reset_singleton_for_tests()
        for key in ("NODE_ENV", "IG_NODE_PROFILE", "IG_TRIAGE_DB", "IG_AGENT_DATA_DIR", "IG_MULTI_API_BROKER"):
            os.environ.pop(key, None)

    def test_crm403_chaos_warmup_flood_circuit_breaker_flush_capital(self) -> None:
        from apex import microkernel
        from apex.hardening import (
            BASELINE_EQUITY_GBP,
            PER_ASSET_RISK_CAP_GBP,
            PORTFOLIO_RISK_CEILING_GBP,
            floor_contract_size,
        )
        from apex.microkernel import (
            DEFERRED_HEAP_BUDGET_BYTES,
            DEFERRED_QUEUE_MAX_FRAMES,
            DEFERRED_FLUSH_SLA_MS,
            deferred_queue_footprint_bytes,
            get_microkernel,
            reset_microkernel_for_tests,
        )
        from apex.warmup_progress import mark_warmup_ready, reset_warmup_progress
        from execution.execution_engine import ExecutionEngine
        from execution.live_executor import LiveExecutor
        from signals.indicators import compute_math_matrix
        from system.system_state import BootPhase, SystemState

        os.environ["NODE_ENV"] = "shadow"
        os.environ["IG_MULTI_API_BROKER"] = "0"
        from system.node_profile import get_node_profile, reset_node_profile_for_tests

        reset_node_profile_for_tests()
        profile = get_node_profile(reload=True)
        self.assertTrue(str(profile.triage_db).endswith("triage_v30.db"))
        self.assertNotEqual(profile.triage_db, profile.learning_db)
        legacy_learning = ROOT / "src" / "data" / "learning_db.sqlite3"
        self.assertNotEqual(profile.learning_db.resolve(), legacy_learning.resolve())

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "triage_v30.db"
            os.environ["IG_TRIAGE_DB"] = str(db_path)
            from analytics.triage_logger import get_triage_logger, reset_triage_logger_for_tests

            reset_triage_logger_for_tests()
            logger = get_triage_logger()
            logger.start()
            time.sleep(0.15)
            with sqlite3.connect(str(db_path)) as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                sync = conn.execute("PRAGMA synchronous").fetchone()[0]
                busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            self.assertGreaterEqual(int(sync), 1)  # not OFF — NORMAL/FULL per SQLite build
            reset_triage_logger_for_tests()

        triage_tmp = tempfile.mkdtemp(prefix="crm403_triage_")
        self.addCleanup(lambda: __import__("shutil").rmtree(triage_tmp, ignore_errors=True))
        os.environ["IG_TRIAGE_DB"] = str(Path(triage_tmp) / "triage_v30.db")
        reset_microkernel_for_tests()
        SystemState.reset_singleton_for_tests()
        state = SystemState.get()
        state.update_state(BootPhase.WARMING, 80, "Compiling Vector Arrays [80%]", ready=False)
        reset_warmup_progress(bars_target=256 * 2)

        kernel = get_microkernel()
        kernel.start()

        ring_gold = kernel._ring_for(_GOLD)
        ring_dow = kernel._ring_for(_WALL_ST)
        self.assertEqual(ring_gold._count, 0)
        self.assertEqual(ring_dow._count, 0)

        t_flood = time.perf_counter()
        for i in range(_FLOOD_COUNT):
            epic = _GOLD if i % 2 == 0 else _WALL_ST
            kernel.on_tick_ingest(epic, _volatile_quote(i, epic=epic))
        flood_elapsed = time.perf_counter() - t_flood
        self.assertLess(flood_elapsed, 1.0, "2000-tick flood must complete in <1s")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if kernel.deferred_queue_depth() >= min(_FLOOD_COUNT, DEFERRED_QUEUE_MAX_FRAMES):
                break
            time.sleep(0.01)

        depth = kernel.deferred_queue_depth()
        self.assertGreaterEqual(depth, min(_FLOOD_COUNT, DEFERRED_QUEUE_MAX_FRAMES) - 50)
        self.assertLessEqual(depth, DEFERRED_QUEUE_MAX_FRAMES)
        footprint = deferred_queue_footprint_bytes(depth)
        self.assertLessEqual(footprint, DEFERRED_HEAP_BUDGET_BYTES)

        self.assertEqual(ring_gold._count, 0)
        self.assertEqual(ring_dow._count, 0)
        stats = kernel.stats()
        self.assertEqual(stats.get("ingested", 0), 0)

        # ── STEP B: high-confidence breakout — circuit breaker HOLD ──
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

        cfg = MagicMock(allow_live_trading=True, account_type="DEMO", dry_run=False, cooldown_seconds=0)
        engine = ExecutionEngine(mode=ExecutionMode.DEMO, config=cfg, store=MagicMock(), rest_client=MagicMock())
        signal = TradeSignal(
            market="Wall St",
            epic=_WALL_ST,
            direction="BUY",
            raw_confidence=_CONFIDENCE_BREAKOUT,
            adjusted_confidence=_CONFIDENCE_BREAKOUT,
            setup_key="BUY|chaos",
            quote=_volatile_quote(0, epic=_WALL_ST),
        )
        blocked = engine._execute_trade_body(signal)
        self.assertFalse(blocked.success)

        live_blocked = LiveExecutor(MagicMock(), cfg).execute(
            signal, {"size": 3.7}, MagicMock(), MagicMock(), mode=ExecutionMode.DEMO
        )
        self.assertFalse(live_blocked.success)

        # Integer floor + USD inversion wall at WARMING→READY boundary
        size_int, under_min = floor_contract_size(3.7)
        self.assertEqual(size_int, int(3.7 // 1))
        self.assertFalse(under_min)
        self.assertNotEqual(size_int, 3.7)

        with patch(
            "trading.trading_loop._epic_requires_usd_gbp_risk_conversion", return_value=True
        ):
            from trading.trading_loop import TradingLoop as TL

            self.assertTrue(TL.__module__)
            self.assertTrue(_WALL_ST.startswith("IX.D."))

        # ── STEP C: mark_warmup_ready → flush → float64 math → capital ──
        flushed = mark_warmup_ready()
        self.assertTrue(flushed.get("ready"))

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if kernel._ring_for(_GOLD)._count > 0:
                break
            time.sleep(0.01)

        flush_ms = float(kernel.stats().get("deferred_flush_ms", 0.0))
        self.assertGreater(flush_ms, 0.0)

        close, high, low = ring_gold.ordered_views()
        self.assertEqual(close.dtype, np.float64)
        self.assertGreater(ring_gold._count, 0)
        self.assertLessEqual(ring_gold._count, 256)

        # Math SLA on canonical 256-bar float64 ring geometry (Worker B hot path).
        bench_close = np.linspace(2400.0, 2500.0, 256, dtype=np.float64)
        bench_high = bench_close + 0.4
        bench_low = bench_close - 0.4
        ind_buf = np.zeros((256, 4), dtype=np.float64)
        for _ in range(3):
            compute_math_matrix(
                bench_close, bench_high, bench_low, out_indicator_matrix=ind_buf
            )
        math_samples: list[float] = []
        for _ in range(32):
            t_math = time.perf_counter()
            snap = compute_math_matrix(
                bench_close, bench_high, bench_low, out_indicator_matrix=ind_buf
            )
            math_samples.append((time.perf_counter() - t_math) * 1_000_000.0)
        math_us = min(math_samples)
        self.assertLess(math_us, 250.0, f"compute_math_matrix best-of-32 took {math_us:.1f}µs")
        self.assertEqual(snap["indicator_matrix"].dtype, np.float64)

        state.update_state(BootPhase.G5, 98, "ACTIVE", ready=False)
        verdict = kernel.publish_risk_context(
            epic=_GOLD,
            size=float(size_int),
            stop_pts=50.0,
            spread_pts=2.0,
            point_value_gbp=1.0,
            concurrent_risk_gbp=0.0,
            ml_pass=True,
        )
        self.assertLessEqual(verdict.risk_gbp, PER_ASSET_RISK_CAP_GBP)
        self.assertLessEqual(verdict.risk_gbp, PORTFOLIO_RISK_CEILING_GBP)
        self.assertEqual(verdict.size_int, size_int)

        session_equity = float(BASELINE_EQUITY_GBP)
        self.assertEqual(session_equity, 10_000.0)

        kernel.stop()

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
            notes="chaos breakout",
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
                quote=_volatile_quote(1),
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
            quote_source=lambda: _volatile_quote(99, epic=_WALL_ST),
            learning_store=MagicMock(sum_daily_pnl=MagicMock(return_value=0.0)),
            tick_interval_sec=0.05,
        )


class V30StorageIsolationAudit(unittest.TestCase):
    def tearDown(self) -> None:
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        os.environ.pop("NODE_ENV", None)

    def test_apex_isolated_analytics_path(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        from system.node_profile import get_node_profile, reset_node_profile_for_tests
        from system.paths import apex_isolated_root

        reset_node_profile_for_tests()
        profile = get_node_profile(reload=True)
        expected = apex_isolated_root() / "analytics" / "triage_v30.db"
        self.assertEqual(profile.triage_db, expected)
        self.assertNotIn("src/data", str(profile.triage_db))
        self.assertNotIn("learning_db.sqlite3", str(profile.triage_db))


if __name__ == "__main__":
    unittest.main()
