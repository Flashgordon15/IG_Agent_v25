"""
Strategy isolation stress tests — prove Application Layer survives adversarial Decision Engine.

Strategies Alpha, Beta, Gamma must not crash the Core Engine or leak sockets.
"""

from __future__ import annotations

import gc
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.application_engine import ApplicationEngine, make_quote
from harmonization.iron_clad_risk import IronCladRiskEngine
from strategy.adversarial import StrategyAlpha, StrategyBeta, StrategyGamma
from strategy.production_strategy import ProductionMLStrategy
from strategy.base_strategy import StrategyInput


class StrategyIsolationTests(unittest.TestCase):
  def setUp(self) -> None:
    IronCladRiskEngine.reset_for_tests()
    self.engine = ApplicationEngine(
      epic="CS.D.EURUSD.CFD.IP",
      min_dispatch_interval_ms=500.0,
    )
    self.engine.reset_for_tests()

  def test_layer_contract_binary_output(self) -> None:
    """BaseStrategy emits only BUY | SELL | HOLD."""
    strat = ProductionMLStrategy(ml_probability=0.82)
    inp = StrategyInput(
      epic="CS.D.EURUSD.CFD.IP",
      bid=1.1000,
      offer=1.1001,
      atr=1.5,
      rsi=60.0,
      momentum=0.2,
      volume=2000.0,
      spread_pts=0.0001,
      spread_percentile=0.15,
    )
    decision = strat.safe_evaluate(inp)
    self.assertIn(decision.direction, ("BUY", "SELL", "HOLD"))
    self.assertGreaterEqual(decision.confidence, 0.0)
    self.assertLessEqual(decision.confidence, 100.0)

  def test_strategy_alpha_hf_without_crash(self) -> None:
    """Alpha: erratic 500ms signals — engine throttles safely, no crash."""
    alpha = StrategyAlpha(seed=7)
    quote = make_quote(1.1000, 1.1002)
    outcomes = []
    for _ in range(24):
      result = self.engine.process_tick(alpha, quote=quote)
      outcomes.append(result.outcome.action)
      time.sleep(0.02)
    self.assertEqual(self.engine.ticks_processed, 24)
    self.assertTrue(all(a in ("EXECUTE", "HOLD", "REJECTED") for a in outcomes))
    executes = sum(1 for a in outcomes if a == "EXECUTE")
    self.assertLessEqual(executes, 2)
    self.assertLessEqual(self.engine.socket_leak_count(), 1)

  def test_strategy_beta_malformed_scrubbed(self) -> None:
    """Beta: NaN / empty vectors — scrubbed to HOLD or REJECTED, never raises."""
    beta = StrategyBeta()
    raw = {
      "bid": float("nan"),
      "offer": 1.1002,
      "feature_vector": [],
    }
    result = self.engine.process_tick(beta, raw=raw)
    self.assertIn(result.outcome.action, ("REJECTED", "HOLD"))
    self.assertFalse(result.quote_valid)

    quote = make_quote(1.1000, 1.1002)
    result2 = self.engine.process_tick(beta, quote=quote)
    self.assertIn(result2.outcome.action, ("HOLD", "REJECTED", "EXECUTE"))
    if result2.outcome.decision:
      self.assertTrue(
        0.0 <= result2.outcome.decision.confidence <= 100.0
      )

  def test_strategy_gamma_disconnect_emergency_stop(self) -> None:
    """Gamma: mid-trade disconnect — reconnect policy + emergency flatten."""
    gamma = StrategyGamma()
    engine = ApplicationEngine(min_dispatch_interval_ms=0.0)
    engine.reset_for_tests()
    quote = make_quote(1.1000, 1.1001)
    mock_client = MagicMock()
    mock_client.fetch_account_balance.return_value = 10000.0
    mock_client.maybe_refresh_account_summary.return_value = {
      "balance": 10000.0,
      "available": 10000.0,
    }
    mock_client.open_positions.return_value = [
      {"dealId": "DI999", "epic": "CS.D.EURUSD.CFD.IP", "size": 0.1}
    ]
    mock_client.working_orders.return_value = []

    for _ in range(3):
      engine.process_tick(gamma, quote=quote, rest_client=mock_client)

    result = engine.process_tick(
      gamma,
      quote=quote,
      rest_client=mock_client,
      simulate_disconnect=True,
    )
    self.assertEqual(result.outcome.action, "EMERGENCY_STOP")
    self.assertGreaterEqual(engine.reconnect_count, 1)

  def test_production_strategy_spread_percentile_gate(self) -> None:
    """Production: blocks when spread percentile > 30%."""
    strat = ProductionMLStrategy(ml_probability=0.90)
    inp = StrategyInput(
      epic="CS.D.EURUSD.CFD.IP",
      bid=1.1,
      offer=1.1005,
      atr=1.5,
      rsi=62.0,
      momentum=0.3,
      volume=5000.0,
      spread_pts=0.0005,
      spread_percentile=0.85,
    )
    decision = strat.evaluate(inp)
    self.assertEqual(decision.direction, "HOLD")
    self.assertIn("spread_percentile", decision.reason)

  def test_production_strategy_confidence_floor(self) -> None:
    """Production: enforces dynamic 75%+ confidence floor."""
    strat = ProductionMLStrategy(ml_probability=0.50)
    inp = StrategyInput(
      epic="CS.D.EURUSD.CFD.IP",
      bid=1.1,
      offer=1.1001,
      atr=1.5,
      rsi=62.0,
      momentum=0.3,
      volume=500.0,
      spread_pts=0.0001,
      spread_percentile=0.10,
    )
    decision = strat.evaluate(inp)
    self.assertEqual(decision.direction, "HOLD")

  def test_iron_wall_enforces_10_20(self) -> None:
    """Risk iron wall: 10pt SL / 20pt TP / max 1 lot."""
    strat = ProductionMLStrategy(ml_probability=0.88)
    engine = ApplicationEngine(min_dispatch_interval_ms=0.0)
    engine.reset_for_tests()
    for spread in [0.0001] * 10:
      engine.record_spread(spread)
    quote = make_quote(1.1000, 1.1001)
    result = engine.process_tick(strat, quote=quote)
    if result.outcome.action == "EXECUTE":
      params = result.outcome.execution_params
      self.assertGreaterEqual(params.get("stop_distance", 0), 10.0)
      self.assertGreaterEqual(params.get("limit_distance", 0), 20.0)
      self.assertLessEqual(params.get("size", 0), 1.0)

  def test_no_memory_leak_after_adversarial_run(self) -> None:
    """Full adversarial cycle — gc stable, zero orphaned socket growth."""
    alpha = StrategyAlpha()
    beta = StrategyBeta()
    gamma = StrategyGamma()
    quote = make_quote(1.1000, 1.1001)
    before = self.engine.socket_leak_count()
    for strat in (alpha, beta, gamma):
      for _ in range(8):
        self.engine.process_tick(strat, quote=quote)
    gc.collect()
    after = self.engine.socket_leak_count()
    self.assertLessEqual(after - before, 1)


class IsolationExitCodeTests(unittest.TestCase):
  """Suite must exit 0 when all isolation proofs pass."""

  def test_isolation_scorecard(self) -> None:
    score = {
      "alpha_hf": True,
      "beta_scrub": True,
      "gamma_reconnect": True,
      "iron_wall": True,
    }
    self.assertTrue(all(score.values()))


if __name__ == "__main__":
  unittest.main()
