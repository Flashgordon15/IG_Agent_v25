"""Unit tests — Alpha-Optimised Trailing Engine (mock positions + quotes)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.alpha_trail import (
    ANTI_REGRET_PROFIT_PIPS,
    ANTI_REGRET_STOP_OFFSET_PIPS,
    AlphaOptimisedTrailEngine,
    AlphaTrailPosition,
    apply_capital_harvest_contract,
    reset_capital_harvest_contract_for_tests,
)
from intelligence.target_engine import (
    TargetSeekingEngine,
    reset_target_engine_for_tests,
)
from system.pnl_math import ig_points_to_price_delta


class AlphaTrailTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_capital_harvest_contract_for_tests()
        reset_target_engine_for_tests()
        self.engine = AlphaOptimisedTrailEngine(
            base_atr_mult=0.55,
            tighten_atr_mult=0.30,
            run_atr_mult=0.85,
            profit_tighten_pts=10.0,
        )

    def tearDown(self) -> None:
        reset_capital_harvest_contract_for_tests()
        reset_target_engine_for_tests()

    def _pos(self) -> AlphaTrailPosition:
        return AlphaTrailPosition(
            epic="IX.D.NIKKEI.IFM.IP",
            side="BUY",
            entry=39000.0,
            stop=38960.0,
            target=39080.0,
            atr_pts=40.0,
        )

    def test_trail_advances_in_profit_momentum_regime(self) -> None:
        pos = self._pos()
        v = self.engine.compute(
            pos,
            bid=39008.0,
            offer=39015.0,
            micro_regime="MOMENTUM_UP",
            trigger_atr_mult=0.20,
        )
        self.assertGreater(v.profit_pts, 0.0)
        self.assertGreaterEqual(v.atr_multiple, 0.55)
        self.assertIsNotNone(v.proposed_stop)
        assert v.proposed_stop is not None
        self.assertGreater(v.proposed_stop, pos.stop)

    def test_tighten_after_profit_threshold(self) -> None:
        pos = self._pos()
        v = self.engine.compute(
            pos,
            bid=39025.0,
            offer=39032.0,
            micro_regime="NEUTRAL",
            trigger_atr_mult=0.25,
        )
        self.assertTrue(v.tighten_mode)
        self.assertAlmostEqual(v.atr_multiple, 0.30)

    def test_no_trail_without_atr(self) -> None:
        pos = AlphaTrailPosition(
            epic="EPIC",
            side="BUY",
            entry=100.0,
            stop=99.0,
            target=102.0,
            atr_pts=0.0,
        )
        v = self.engine.compute(pos, bid=100.5, offer=100.7)
        self.assertIsNone(v.proposed_stop)
        self.assertEqual(v.detail, "atr_unavailable")

    def test_anti_regret_be_at_15_pips(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        entry = 39000.0
        stop = 38960.0
        offset = ig_points_to_price_delta(epic, ANTI_REGRET_STOP_OFFSET_PIPS)
        profit_pts = ig_points_to_price_delta(epic, ANTI_REGRET_PROFIT_PIPS)
        px = entry + profit_pts
        proposed, detail = apply_capital_harvest_contract(
            epic=epic,
            side="BUY",
            entry=entry,
            stop=stop,
            px=px,
            profit_pts=profit_pts,
            proposed_stop=None,
            deal_id="D1",
        )
        self.assertIsNotNone(proposed)
        assert proposed is not None
        self.assertAlmostEqual(proposed, entry + offset, places=2)
        self.assertIn("ANTI_REGRET_BE", detail)

    def test_two_r_lock_at_double_risk(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        entry = 39000.0
        stop = 38960.0
        risk = entry - stop
        px = entry + risk * 2.0 + 1.0
        proposed, detail = apply_capital_harvest_contract(
            epic=epic,
            side="BUY",
            entry=entry,
            stop=stop,
            px=px,
            profit_pts=px - entry,
            proposed_stop=None,
            deal_id="D2",
        )
        self.assertIsNotNone(proposed)
        assert proposed is not None
        self.assertGreaterEqual(proposed, entry + risk)
        self.assertIn("TWO_R_LOCK", detail)

    def test_parabolic_snap_when_milestone_active(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        entry = 1.08500
        stop = 1.08450
        profit_pts = 0.00150
        px = entry + profit_pts
        proposed, detail = apply_capital_harvest_contract(
            epic=epic,
            side="BUY",
            entry=entry,
            stop=stop,
            px=px,
            profit_pts=profit_pts,
            proposed_stop=None,
            deal_id="D3",
            parabolic_snap_active=True,
            p_day_gbp=760.0,
            lock_floor_gbp=500.0,
        )
        self.assertIsNotNone(proposed)
        assert proposed is not None
        self.assertIn("PARABOLIC_SNAP", detail)
        self.assertGreater(proposed, entry)

    def test_target_engine_milestone_snap_threshold(self) -> None:
        te = TargetSeekingEngine(target_daily_gbp=1000.0)
        te.last_p_day = 749.0
        self.assertFalse(te.capital_harvest_milestone_snap_active())
        te.last_p_day = 750.0
        self.assertTrue(te.capital_harvest_milestone_snap_active())
        self.assertEqual(te.capital_harvest_lock_floor_gbp(), 500.0)

    def test_compute_welds_harvest_contract(self) -> None:
        pos = self._pos()
        profit_bid = pos.entry + 20.0
        v = self.engine.compute(
            pos,
            bid=profit_bid,
            offer=profit_bid + 7.0,
            micro_regime="NEUTRAL",
            trigger_atr_mult=0.10,
        )
        self.assertIsNotNone(v.proposed_stop)
        assert v.proposed_stop is not None
        self.assertGreater(v.proposed_stop, pos.entry)
        self.assertIn("harvest", v.detail)


if __name__ == "__main__":
    unittest.main()
