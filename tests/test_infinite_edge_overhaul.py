"""Infinite Edge Overhaul — macro radar, OBI schema, velocity RSI, shadow engine."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.telemetry_schema import OrderBookDepthPayload, validate_order_book_depth
from intelligence.macro_radar import (
    collect_macro_snapshot,
    macro_confidence_adjustment,
    reset_macro_radar_for_tests,
)
from intelligence.microstructure import MicrostructureClassifier, effective_entry_rsi_ceiling
from intelligence.order_book_imbalance import compute_obi_ratio, obi_institutional_flag
from trading.shadow_executor import ShadowExecutor, reset_shadow_executor_for_tests, shadow_mode_active


class InfiniteEdgeOverhaulTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_macro_radar_for_tests()
        reset_shadow_executor_for_tests()

    def test_velocity_disables_rsi_ceiling(self) -> None:
        clf = MicrostructureClassifier()
        epic = "CS.D.EURUSD.CFD.IP"
        t0 = time.time() - 0.15
        for i in range(20):
            clf.record_tick(
                epic,
                bid=1.0850 + i * 0.00001,
                offer=1.08508 + i * 0.00001,
                ts=t0 + i * 0.008,
            )
        self.assertTrue(clf.velocity_engaged(epic))
        self.assertEqual(clf.effective_entry_rsi_ceiling(epic, base_ceiling=85.0), 99.0)

    def test_flat_ticks_keep_rsi_ceiling(self) -> None:
        clf = MicrostructureClassifier()
        epic = "CS.D.EURUSD.CFD.IP"
        t0 = time.time() - 5.0
        for i in range(8):
            clf.record_tick(epic, bid=1.0850, offer=1.08508, ts=t0 + i * 0.5)
        self.assertFalse(clf.velocity_engaged(epic))
        self.assertEqual(clf.effective_entry_rsi_ceiling(epic, base_ceiling=85.0), 85.0)

    def test_warmup_blend_reduces_first_live_jump(self) -> None:
        clf = MicrostructureClassifier()
        epic = "IX.D.DOW.IFM.IP"
        bars = [
            {
                "time": "2026-06-16T10:00:00",
                "high": 39000.0,
                "low": 38990.0,
                "bid_close": 38995.0,
                "offer_close": 39005.0,
            }
            for _ in range(20)
        ]
        clf.seed_from_ohlc_bars(epic, bars)
        before = clf.classify(epic).confidence
        clf.record_tick(epic, bid=39500.0, offer=39510.0, source="live")
        after = clf.classify(epic).confidence
        self.assertLess(abs(after - before), 0.35)

    def test_order_book_depth_obi_ratio(self) -> None:
        payload = OrderBookDepthPayload(
            epic="CS.D.EURUSD.CFD.IP",
            ts=time.time(),
            bid_levels=[{"price": 1.0850, "size": 3.0}],
            ask_levels=[{"price": 1.0851, "size": 1.0}],
        )
        ratio = compute_obi_ratio(payload)
        self.assertGreater(ratio, 0.4)
        self.assertEqual(obi_institutional_flag(ratio, threshold=0.5), "EXTREME_BUY_STACK")
        normalized = validate_order_book_depth(payload.model_dump())
        self.assertIn("obi_ratio", normalized)

    def test_macro_radar_collects_weights(self) -> None:
        snap = collect_macro_snapshot()
        self.assertEqual(len(snap.feature_weights), 5)
        boosted = macro_confidence_adjustment(
            "CS.D.EURUSD.CFD.IP", 0.5, "MOMENTUM_UP"
        )
        self.assertGreaterEqual(boosted, 0.5)

    def test_shadow_mode_flag(self) -> None:
        with patch.dict(os.environ, {"IG_AGENT_MODE": "SHADOW"}):
            self.assertTrue(shadow_mode_active())
        self.assertFalse(shadow_mode_active())

    def test_effective_entry_rsi_module_helper(self) -> None:
        val = effective_entry_rsi_ceiling("CS.D.EURUSD.CFD.IP", base_ceiling=85.0)
        self.assertEqual(val, 85.0)


if __name__ == "__main__":
    unittest.main()
