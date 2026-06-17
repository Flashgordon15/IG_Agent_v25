#!/usr/bin/env python3
"""
Regional Lifecycle Contract Validation Suite — time-travel state controller.

Proves Tokyo window floors, London open scaling, rollover lock, and Superjet
drawdown circuit breaker contracts across UK session boundaries.

Run:
  PYTHONPATH=src python3 -m pytest tests/stress/regional_lifecycle.py -v
  PYTHONPATH=src python3 tests/stress/regional_lifecycle.py
"""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.liquidity_wave import (
    NIKKEI_EPIC,
    apply_microstructure_wave,
    effective_microstructure_confidence_floor,
    in_tokyo_momentum_window,
    overnight_volatility_size_multiplier,
    resolve_liquidity_wave,
)
from intelligence.premium_overnight import in_rollover_lock, night_matrix_session_allowed
from stress.time_controller import VirtualClock
from system.config import Config
from system.superjet_drawdown_guard import (
    MAX_DAILY_DRAWDOWN_GBP,
    check_and_enforce_async,
    is_frozen,
    reset_superjet_drawdown_guard_for_tests,
    telemetry_snapshot,
)
from trading.entry_protection import check_session_blackout

_LONDON = ZoneInfo("Europe/London")
GOLD = "CS.D.CFPGOLD.CFP.IP"


def _cfg() -> Config:
    return Config(
        _data={
            "intelligence_layer": {
                "liquidity_wave": {
                    "tokyo_momentum_start": "01:00",
                    "tokyo_momentum_end": "04:00",
                    "tokyo_nikkei_micro_floor": 0.65,
                    "premium_defensive_micro_floor": 0.85,
                    "overnight_half_scale_multiplier": 0.50,
                },
                "autopilot_scaling": {"enabled": True, "max_epic_bonus": 2},
            },
            "entry_protection": {
                "enabled": True,
                "session_blackout_enabled": True,
                "gold_epic": GOLD,
                "premium_overnight": {
                    "enabled": True,
                    "lockdown_permanent": True,
                    "epics": [GOLD, "IX.D.DOW.IFM.IP", NIKKEI_EPIC, "CS.D.EURUSD.CFD.IP"],
                    "rollover_lock_start": "21:58",
                    "rollover_lock_end": "22:05",
                    "overnight_session_start": "22:00",
                    "overnight_session_end": "06:00",
                },
            },
        }
    )


class RegionalLifecycleValidator:
    """Automated UK session boundary contract checker."""

    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg or _cfg()

    def validate_tokyo_window(self, clock: VirtualClock) -> dict[str, float | str | bool]:
        clock.jump_to(2, 30)
        now = clock.now
        assert in_tokyo_momentum_window(now=now, config=self._cfg)
        nikkei_floor, _ = effective_microstructure_confidence_floor(
            NIKKEI_EPIC, now=now, config=self._cfg
        )
        gold_floor, _ = effective_microstructure_confidence_floor(
            GOLD, now=now, config=self._cfg
        )
        half_mult, _ = overnight_volatility_size_multiplier(
            0.72, epic=NIKKEI_EPIC, config=self._cfg, now=now
        )
        return {
            "nikkei_floor": nikkei_floor,
            "gold_floor": gold_floor,
            "half_scale_mult": half_mult,
            "tokyo_active": True,
        }

    def validate_london_open(self, clock: VirtualClock) -> dict[str, float | str]:
        clock.jump_to(9, 0)
        now = clock.now
        wave = resolve_liquidity_wave(now=now)
        base_conf = 0.70
        boosted, note = apply_microstructure_wave(
            base_conf, "MOMENTUM_UP", now=now
        )
        boosted_slots = int(2 * wave.autopilot_multiplier)
        return {
            "confidence_multiplier": wave.confidence_multiplier,
            "autopilot_multiplier": wave.autopilot_multiplier,
            "ml_boosted_conf": boosted,
            "ml_boost_note": note,
            "position_cap_scaled": float(boosted_slots),
        }

    def validate_rollover_lock(self, clock: VirtualClock) -> dict[str, bool | str]:
        clock.jump_to(22, 0)
        now = clock.now
        blocked, reason = check_session_blackout(GOLD, self._cfg, now=now)
        allowed, allow_reason = night_matrix_session_allowed(
            GOLD, config=self._cfg, now=now
        )
        return {
            "in_rollover": in_rollover_lock(now=now, config=self._cfg),
            "blocked": blocked,
            "reason": reason,
            "routing_allowed": allowed,
            "allow_reason": allow_reason,
        }

    def validate_drawdown_breaker(self) -> dict[str, Any]:
        reset_superjet_drawdown_guard_for_tests()
        flatten_called = False
        stop_called = False

        class _SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=True, name=None):
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self) -> None:
                if self._target:
                    self._target(*self._args, **self._kwargs)

            def join(self, timeout=None) -> None:
                return None

        with patch(
            "system.superjet_drawdown_guard._resolve_daily_pnl_gbp",
            return_value=-500.0,
        ):
            snap = telemetry_snapshot()
            assert snap["breached"] is True
            with patch(
                "cockpit.emergency.execute_emergency_cockpit_override"
            ) as flatten_mock:
                with patch(
                    "system.shutdown_cleanup.mark_manual_stop"
                ) as stop_mock:
                    with patch(
                        "system.superjet_drawdown_guard.threading.Thread",
                        _SyncThread,
                    ):
                        check_and_enforce_async()
                    flatten_called = flatten_mock.called
                    stop_called = stop_mock.called
        return {
            "breached": snap["breached"],
            "frozen": is_frozen(),
            "flatten_called": flatten_called,
            "manual_stop_called": stop_called,
        }


class RegionalLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = RegionalLifecycleValidator()
        self.clock = VirtualClock(
            start=datetime(2026, 6, 17, 0, 0, tzinfo=_LONDON)
        )

    def tearDown(self) -> None:
        reset_superjet_drawdown_guard_for_tests()

    def test_tokyo_nikkei_65_floor_defensive_85_half_scale(self) -> None:
        r = self.validator.validate_tokyo_window(self.clock)
        self.assertAlmostEqual(r["nikkei_floor"], 0.65)
        self.assertAlmostEqual(r["gold_floor"], 0.85)
        self.assertAlmostEqual(r["half_scale_mult"], 0.50)

    def test_london_open_35pct_ml_and_15x_slots(self) -> None:
        r = self.validator.validate_london_open(self.clock)
        self.assertAlmostEqual(r["confidence_multiplier"], 1.35)
        self.assertAlmostEqual(r["autopilot_multiplier"], 1.5)
        self.assertGreater(r["ml_boosted_conf"], 0.70)
        self.assertIn("london_open", r["ml_boost_note"])
        self.assertAlmostEqual(r["position_cap_scaled"], 3.0)

    def test_rollover_2158_2205_blocks_all_routing(self) -> None:
        r = self.validator.validate_rollover_lock(self.clock)
        self.assertTrue(r["in_rollover"])
        self.assertTrue(r["blocked"])
        self.assertIn("rollover lock", str(r["reason"]).lower())
        self.assertFalse(r["routing_allowed"])
        self.assertIn("rollover lock", str(r["allow_reason"]).lower())

    def test_rollover_clear_at_2205(self) -> None:
        self.clock.jump_to(22, 5)
        blocked, _ = check_session_blackout(
            GOLD, _cfg(), now=self.clock.now
        )
        self.assertFalse(blocked)

    def test_drawdown_500_triggers_flatten_and_freeze(self) -> None:
        r = self.validator.validate_drawdown_breaker()
        self.assertTrue(r["breached"])
        self.assertTrue(r["frozen"])
        self.assertTrue(r["flatten_called"])
        self.assertTrue(r["manual_stop_called"])
        self.assertEqual(MAX_DAILY_DRAWDOWN_GBP, 500.0)

    def test_virtual_clock_walks_boundaries(self) -> None:
        end = datetime(2026, 6, 17, 4, 0, tzinfo=_LONDON)
        points = list(self.clock.walk_until(end))
        self.assertGreater(len(points), 40)
        tokyo_points = [p for p in points if in_tokyo_momentum_window(now=p)]
        self.assertGreater(len(tokyo_points), 30)


if __name__ == "__main__":
    unittest.main()
