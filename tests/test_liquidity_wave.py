"""Liquidity wave + asset priority unit tests."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.liquidity_wave import (
    LiquidityPhase,
    NIKKEI_EPIC,
    apply_microstructure_wave,
    effective_microstructure_confidence_floor,
    in_tokyo_momentum_window,
    overnight_volatility_size_multiplier,
    resolve_liquidity_wave,
)
from intelligence.target_engine import TargetSeekingEngine, reset_target_engine_for_tests
from trading.asset_priority import (
    PRIORITY_ASSET_MATRIX,
    epic_priority_multiplier,
    fuse_qmm_rank_score,
)


class LiquidityWaveTests(unittest.TestCase):
    def test_london_open_peak_window(self) -> None:
        dt = datetime(2026, 6, 16, 9, 0, tzinfo=ZoneInfo("Europe/London"))
        wave = resolve_liquidity_wave(now=dt)
        self.assertEqual(wave.phase, LiquidityPhase.LONDON_OPEN)
        self.assertAlmostEqual(wave.autopilot_multiplier, 1.5)

    def test_midday_lull_premium_only(self) -> None:
        dt = datetime(2026, 6, 16, 12, 0, tzinfo=ZoneInfo("Europe/London"))
        wave = resolve_liquidity_wave(now=dt)
        self.assertEqual(wave.phase, LiquidityPhase.MIDDAY_LULL)
        self.assertTrue(wave.entry_premium_only)

    def test_microstructure_boost_in_ny_open(self) -> None:
        dt = datetime(2026, 6, 16, 15, 0, tzinfo=ZoneInfo("Europe/London"))
        conf, note = apply_microstructure_wave(0.7, "MOMENTUM_UP", now=dt)
        self.assertGreater(conf, 0.7)
        self.assertIn("new_york_open", note)

    def test_tokyo_momentum_window_phase(self) -> None:
        dt = datetime(2026, 6, 17, 2, 30, tzinfo=ZoneInfo("Europe/London"))
        self.assertTrue(in_tokyo_momentum_window(now=dt))
        wave = resolve_liquidity_wave(now=dt)
        self.assertEqual(wave.phase, LiquidityPhase.TOKYO_MOMENTUM)

    def test_tokyo_nikkei_floor_65_defensive_epics_85(self) -> None:
        dt = datetime(2026, 6, 17, 2, 30, tzinfo=ZoneInfo("Europe/London"))
        nikkei_floor, nikkei_reason = effective_microstructure_confidence_floor(
            NIKKEI_EPIC, now=dt
        )
        gold_floor, _ = effective_microstructure_confidence_floor(
            "CS.D.CFPGOLD.CFP.IP", now=dt
        )
        self.assertAlmostEqual(nikkei_floor, 0.65)
        self.assertAlmostEqual(gold_floor, 0.85)
        self.assertIn("tokyo", nikkei_reason)

    def test_overnight_half_scale_lot_multiplier(self) -> None:
        dt = datetime(2026, 6, 17, 2, 30, tzinfo=ZoneInfo("Europe/London"))
        cfg = {
            "entry_protection": {
                "premium_overnight": {
                    "enabled": True,
                    "epics": [NIKKEI_EPIC],
                    "overnight_session_start": "22:00",
                    "overnight_session_end": "06:00",
                }
            }
        }
        half, half_reason = overnight_volatility_size_multiplier(
            0.72, epic=NIKKEI_EPIC, config=cfg, now=dt
        )
        full, full_reason = overnight_volatility_size_multiplier(
            0.88, epic=NIKKEI_EPIC, config=cfg, now=dt
        )
        self.assertAlmostEqual(half, 0.50)
        self.assertAlmostEqual(full, 1.0)
        self.assertIn("half_scale", half_reason)
        self.assertIn("full_scale", full_reason)


class AssetPriorityTests(unittest.TestCase):
    def test_priority_epics_configured(self) -> None:
        self.assertIn("IX.D.DAX.IFM.IP", PRIORITY_ASSET_MATRIX)
        self.assertIn("IX.D.DOW.IFM.IP", PRIORITY_ASSET_MATRIX)
        self.assertGreater(epic_priority_multiplier("IX.D.DAX.IFM.IP"), 1.0)

    def test_fuse_rank_without_intelligence_layer(self) -> None:
        fused = fuse_qmm_rank_score("IX.D.DOW.IFM.IP", 1.0)
        self.assertGreaterEqual(fused, 1.0)


class TargetMidnightResetTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_target_engine_for_tests()

    def test_uk_midnight_resets_session_state(self) -> None:
        engine = TargetSeekingEngine()
        engine._session_day = "2026-06-15"
        engine.last_p_day = 850.0
        engine.capital_preservation = True
        engine.mission_accomplished = True
        with patch.object(engine, "_uk_today", return_value="2026-06-16"):
            reset = engine._maybe_reset_uk_midnight()
        self.assertTrue(reset)
        self.assertEqual(engine.last_p_day, 0.0)
        self.assertFalse(engine.capital_preservation)


if __name__ == "__main__":
    unittest.main()
