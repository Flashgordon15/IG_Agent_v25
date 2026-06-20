"""
v30 Apex Monolith — single integration test for warmup mutex, deferred flush,
and trading circuit breaker (Pillar 3).
"""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.types import ExecutionMode, TradeSignal
from trading.trading_loop import TradingLoop


def _quote(bid: float = 100.0, offer: float | None = None) -> Quote:
    off = offer if offer is not None else bid + 0.5
    return Quote(datetime(2026, 6, 18, 12, 0), bid, off)


class ApexWarmupMonolithE2ETest(unittest.TestCase):
  """End-to-end: mutex defer → seed → flush → circuit breaker → unblock."""

  def tearDown(self) -> None:
    from apex.microkernel import reset_microkernel_for_tests
    from apex.warmup_progress import reset_warmup_for_tests
    from system.system_state import SystemState

    reset_microkernel_for_tests()
    reset_warmup_for_tests()
    SystemState.reset_singleton_for_tests()

  def test_warmup_mutex_flush_and_execution_circuit_breaker(self) -> None:
    from apex import microkernel
    from apex.microkernel import get_microkernel, reset_microkernel_for_tests
    from apex.warmup_progress import (
      mark_warmup_ready,
      reset_warmup_for_tests,
      reset_warmup_progress,
    )
    from execution.execution_engine import ExecutionEngine
    from execution.live_executor import LiveExecutor
    from system.system_state import BootPhase, SystemState

    epic = "CS.D.EURUSD.CFD.IP"
    market = "EUR/USD"

    # ── Phase A: warming mutex — live ticks deferred, seed owns the ring ──
    reset_microkernel_for_tests()
    reset_warmup_for_tests()
    SystemState.reset_singleton_for_tests()
    state = SystemState.get()
    state.update_state(BootPhase.WARMING, 10, "Compiling Vector Arrays", ready=False)
    reset_warmup_progress(bars_target=256)

    kernel = get_microkernel()
    kernel.start()

    live_bids = [200.0, 201.0, 202.0]
    for bid in live_bids:
      kernel.on_tick_ingest(epic, _quote(bid=bid, offer=bid + 0.2))

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
      stats = kernel.stats()
      if stats.get("ingested_deferred", 0) >= len(live_bids):
        break
      time.sleep(0.02)

    stats = kernel.stats()
    self.assertGreaterEqual(
      stats.get("ingested_deferred", 0),
      len(live_bids),
      "Worker A must buffer live ticks during WARMING",
    )
    self.assertEqual(
      stats.get("ingested", 0),
      0,
      "Live ticks must not hit rings before warmup ready",
    )

    ring = kernel._ring_for(epic)
    close_before_seed, _, _ = ring.ordered_views()
    self.assertEqual(len(close_before_seed), 0)

    seed_df = pd.DataFrame(
      {
        "bid": [100.0, 101.0, 102.0],
        "offer": [100.2, 101.2, 102.2],
      }
    )
    signal_engine = MagicMock()
    signal_engine.quote_df.return_value = seed_df
    seeded = kernel.seed_historical_bars_from_engine(
      epic, signal_engine, market, max_bars=3
    )
    self.assertEqual(seeded, 3)

    close_after_seed, _, _ = ring.ordered_views()
    self.assertEqual(len(close_after_seed), 3)
    np.testing.assert_allclose(close_after_seed, [100.1, 101.1, 102.1], rtol=0, atol=1e-9)

    self.assertTrue(microkernel.is_warmup_gate_active())
    self.assertFalse(microkernel.is_warmup_complete())
    self.assertTrue(microkernel.warmup_execution_blocked())

    # ── Phase B: circuit breaker blocks trading + order dispatch ──
    loop = self._build_trading_loop(epic=epic, market=market)
    with patch("system.config_loader.get_config", return_value=loop._config):
      ctx = loop._run_tick_core()
    self.assertIsNotNone(ctx)
    assert ctx is not None
    self.assertFalse(ctx.all_passed)
    self.assertIn("warmup", (ctx.wait_reason or "").lower())
    loop._execution_loop.process_tick.assert_not_called()

    cfg = MagicMock()
    cfg.allow_live_trading = True
    cfg.account_type = "DEMO"
    cfg.dry_run = False
    cfg.cooldown_seconds = 0
    store = MagicMock()
    engine = ExecutionEngine(
      mode=ExecutionMode.DEMO,
      config=cfg,
      store=store,
      rest_client=MagicMock(),
    )
    signal = TradeSignal(
      market=market,
      epic=epic,
      direction="BUY",
      raw_confidence=90.0,
      adjusted_confidence=90.0,
      setup_key="test",
      quote=_quote(),
    )
    blocked = engine._execute_trade_body(signal)
    self.assertFalse(blocked.success)
    self.assertIn("warmup", (blocked.rejection_reason or "").lower())

    live_exec = LiveExecutor(MagicMock(), cfg)
    live_blocked = live_exec.execute(
      signal,
      {"size": 1.0},
      MagicMock(),
      MagicMock(),
      mode=ExecutionMode.DEMO,
    )
    self.assertFalse(live_blocked.success)
    self.assertIn("warmup", (live_blocked.rejection_reason or "").lower())

    # ── Phase C: mark ready — flush deferred ticks in order ──
    snap = mark_warmup_ready()
    self.assertTrue(snap.get("ready"))
    self.assertTrue(microkernel.is_warmup_complete())
    # Arrays ready but Gate5 still owns phase — WARMING blocks until ACTIVE flip.
    self.assertTrue(microkernel.warmup_execution_blocked())

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
      stats = kernel.stats()
      if stats.get("ingested", 0) >= len(live_bids):
        break
      time.sleep(0.02)

    close_final, _, _ = ring.ordered_views()
    self.assertGreaterEqual(len(close_final), 6)
    np.testing.assert_allclose(close_final[:3], [100.1, 101.1, 102.1], rtol=0, atol=1e-9)
    self._assert_ordered_subsequence(
      close_final.tolist(),
      [200.1, 201.1, 202.1],
      "deferred live mids must flush in FIFO order after seed",
    )

    # Simulate Gate5 post-warmup phase advance (orchestrator unpause path).
    state.update_state(BootPhase.G5, 98, "ACTIVE", ready=False)
    self.assertFalse(microkernel.warmup_execution_blocked())

    loop2 = self._build_trading_loop(epic=epic, market=market)
    with patch("system.config_loader.get_config", return_value=loop2._config):
      ctx2 = loop2._run_tick_core()
    self.assertIsNotNone(ctx2)
    assert ctx2 is not None
    self.assertNotEqual((ctx2.wait_reason or "").lower(), "array warmup compile")

    kernel.stop()

    # Packaged shell assets (loadFile protocol)
    splash = ROOT / "build" / "apex-splash.html"
    bundle_err = ROOT / "build" / "apex-bundle-missing.html"
    self.assertTrue(splash.is_file(), "build/apex-splash.html required for Electron loadFile")
    self.assertTrue(
      bundle_err.is_file(),
      "build/apex-bundle-missing.html required for Electron loadFile",
    )
    main_js = (ROOT / "main.js").read_text(encoding="utf-8")
    self.assertIn("loadFile(splashPath)", main_js)
    self.assertNotIn("loadURL(splashHtml)", main_js)
    self.assertNotIn("shadowCockpitPort", main_js)

  def _build_trading_loop(self, *, epic: str, market: str) -> TradingLoop:
    from execution.trading_loop import TickOutcome
    from signals.signal_engine import SignalResult

    config = MagicMock()
    config.allow_live_trading = True
    config.dry_run = False
    config.stop_distance_points = 10.0
    config.min_atr_points = 0.0

    session = MagicMock()
    session.is_session_open.return_value = True
    session.session_open_time = None
    session.on_tick = MagicMock()

    env = MagicMock()
    env.score.return_value = {"score": 85, "factors": {"atr": 50.0}}

    points = MagicMock()
    points.evaluate.return_value = MagicMock(passed=True, detail="ok")

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = SignalResult(
      signal="BUY",
      raw_confidence=92.0,
      adjusted_confidence=92.0,
      learning_delta=0.0,
      setup_key="BUY|test",
      notes="",
      snapshot={"atr": 50.0},
    )
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
        quote=_quote(),
        signal=signal_engine.evaluate.return_value,
        trade_signal=MagicMock(),
        validation=MagicMock(allowed=True, reasons=[], checks={}),
        execution=MagicMock(success=True, action="SUBMITTED", rejection_reason=""),
      )
    )

    store = MagicMock()
    store.sum_daily_pnl.return_value = 0.0

    loop = TradingLoop(
      config=config,
      market=market,
      epic=epic,
      session_manager=session,
      environment_scorer=env,
      points_engine=points,
      signal_engine=signal_engine,
      execution_loop=execution_loop,
      quote_source=lambda: _quote(),
      learning_store=store,
      tick_interval_sec=0.05,
    )
    return loop

  @staticmethod
  def _assert_ordered_subsequence(haystack: list[float], needles: list[float], msg: str) -> None:
    idx = 0
    for target in needles:
      found = False
      while idx < len(haystack):
        if abs(haystack[idx] - target) < 1e-9:
          found = True
          idx += 1
          break
        idx += 1
      if not found:
        raise AssertionError(f"{msg}: missing {target} in {haystack}")


if __name__ == "__main__":
  unittest.main()
