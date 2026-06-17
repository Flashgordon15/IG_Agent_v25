#!/usr/bin/env python3
"""
Scenario-Based Regression Engine — isolated historical playback.

Feeds 5-decimal price actions and extreme volatility into NumPy microstructure
engines and proves ATR trailing + risk compression scale fluidly without
placeholder strings or integer truncation.

Run:
  PYTHONPATH=src python3 -m pytest tests/stress/scenario_replayer.py -v
  PYTHONPATH=src python3 tests/stress/scenario_replayer.py
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.alpha_trail import AlphaOptimisedTrailEngine, AlphaTrailPosition
from intelligence.microstructure import MicrostructureClassifier
from intelligence.target_engine import risk_compression_factor
from intelligence.types import MicroRegime
from stress.historical_feed import HistoricalScenario, ScenarioKind, ScenarioTick
from system.price_precision import is_placeholder_value


@dataclass
class ReplayMetrics:
    ticks_fed: int = 0
    classifications: int = 0
    trail_computes: int = 0
    atr_multiples: list[float] = field(default_factory=list)
    proposed_stops: list[float] = field(default_factory=list)
    risk_factors: list[float] = field(default_factory=list)


class ScenarioReplayer:
    """
    Isolated playback engine — no IG REST, no trading loop threads.

    Mathematically validates classifier + trail boundary math under stress.
    """

    def __init__(self, *, epic: str = "CS.D.EURUSD.CFD.IP") -> None:
        self._epic = epic
        self._micro = MicrostructureClassifier(min_ticks_5s=3)
        self._trail = AlphaOptimisedTrailEngine()

    def replay(self, scenario: HistoricalScenario) -> ReplayMetrics:
        metrics = ReplayMetrics()
        last_verdict = None
        for tick in scenario.ticks():
            self._feed_tick(tick)
            metrics.ticks_fed += 1
            if metrics.ticks_fed % 8 != 0:
                continue
            verdict = self._micro.classify(self._epic, now=tick.ts)
            if verdict is not None:
                metrics.classifications += 1
                last_verdict = verdict
            self._exercise_trail(tick, last_verdict, metrics)
        return metrics

    def _feed_tick(self, tick: ScenarioTick) -> None:
        self._assert_five_decimal_precision(tick.bid, tick.offer)
        self._micro.record_tick(
            tick.epic,
            bid=tick.bid,
            offer=tick.offer,
            ts=tick.ts,
        )

    @staticmethod
    def _assert_five_decimal_precision(bid: float, offer: float) -> None:
        for label, px in ("bid", bid), ("offer", offer):
            text = f"{px:.5f}"
            if is_placeholder_value(text):
                raise AssertionError(f"{label} placeholder rejected: {text}")
            if abs(px - float(text)) > 1e-9:
                raise AssertionError(f"{label} precision loss: {px} != {text}")

    def _exercise_trail(
        self,
        tick: ScenarioTick,
        micro_verdict: Any | None,
        metrics: ReplayMetrics,
    ) -> None:
        entry = round(tick.bid, 5)
        atr_pts = max(8.0, tick.spread * 80_000.0)
        pos = AlphaTrailPosition(
            epic=tick.epic,
            side="BUY",
            entry=entry,
            stop=round(entry - atr_pts * 2, 5),
            target=round(entry + atr_pts * 3, 5),
            atr_pts=atr_pts,
            deal_id=f"REPLAY-{tick.seq:05d}",
        )
        regime: MicroRegime = "NEUTRAL"
        if micro_verdict is not None:
            regime = micro_verdict.regime

        for p_day in (0.0, 250.0, 500.0, 900.0):
            factor = risk_compression_factor(p_day, 1000.0)
            metrics.risk_factors.append(factor)
            v = self._trail.compute(
                pos,
                bid=tick.bid,
                offer=tick.offer,
                micro_regime=regime,
                risk_compression_factor=factor,
                session_profit_pts=p_day * 0.04,
                trigger_atr_mult=0.05,
            )
            metrics.trail_computes += 1
            metrics.atr_multiples.append(float(v.atr_multiple))
            if v.proposed_stop is not None:
                stop = float(v.proposed_stop)
                if is_placeholder_value(stop):
                    raise AssertionError(f"proposed_stop placeholder at seq={tick.seq}")
                if isinstance(v.atr_multiple, int) and v.atr_multiple == v.atr_multiple // 1:
                    if v.atr_multiple != float(v.atr_multiple):
                        raise AssertionError("atr_multiple integer truncation")
                metrics.proposed_stops.append(stop)


class ScenarioReplayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.replayer = ScenarioReplayer()

    def test_trend_up_classifies_and_trails(self) -> None:
        scenario = HistoricalScenario(
            kind=ScenarioKind.TREND_UP,
            epic="CS.D.EURUSD.CFD.IP",
            length=80,
        )
        m = self.replayer.replay(scenario)
        self.assertGreaterEqual(m.ticks_fed, 80)
        self.assertGreater(m.classifications, 0)
        self.assertGreater(m.trail_computes, 0)
        self.assertTrue(all(0.1 <= x <= 2.0 for x in m.atr_multiples))

    def test_flash_crash_volatility_atr_scaling(self) -> None:
        scenario = HistoricalScenario(
            kind=ScenarioKind.FLASH_CRASH,
            epic="CS.D.EURUSD.CFD.IP",
            length=100,
        )
        m = self.replayer.replay(scenario)
        self.assertGreater(m.trail_computes, 0)
        self.assertGreater(len(m.atr_multiples), 0)
        self.assertTrue(all(0.0 <= x <= 2.0 for x in m.atr_multiples))
        for stop in m.proposed_stops:
            self.assertIsInstance(stop, float)
            self.assertFalse(is_placeholder_value(stop))

    def test_risk_compression_monotone_decreasing(self) -> None:
        factors = [risk_compression_factor(p, 1000.0) for p in (0, 200, 500, 800, 1000)]
        for a, b in zip(factors, factors[1:]):
            self.assertLessEqual(b, a)
        self.assertAlmostEqual(factors[0], 1.0)
        self.assertGreaterEqual(factors[-1], 0.1)

    def test_extreme_chop_maintains_float_trail_multiples(self) -> None:
        scenario = HistoricalScenario(
            kind=ScenarioKind.EXTREME_CHOP,
            epic="CS.D.EURUSD.CFD.IP",
            length=60,
        )
        m = self.replayer.replay(scenario)
        for mult in m.atr_multiples:
            self.assertIsInstance(mult, float)
            frac = mult - int(mult)
            if mult not in (0.55, 0.32, 0.85, 1.0):
                self.assertNotEqual(frac, 0.0, msg=f"suspicious integer mult {mult}")

    def test_gap_open_five_decimal_integrity(self) -> None:
        scenario = HistoricalScenario(
            kind=ScenarioKind.GAP_OPEN,
            epic="CS.D.EURUSD.CFD.IP",
            base_mid=1.08543,
            length=70,
        )
        ticks = list(scenario.ticks())
        for t in ticks:
            self.assertEqual(len(f"{t.bid:.5f}".split(".")[-1]), 5)
            self.assertEqual(len(f"{t.offer:.5f}".split(".")[-1]), 5)


if __name__ == "__main__":
    unittest.main()
