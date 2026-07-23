"""Tests for long-trade runner exit profile."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from runtime.long_trade_runner import (
    effective_giveback_ratio,
    effective_target_gbp,
    is_long_runner_active,
    skip_max_age_close_for_runner,
)
from system.config import Config


def _cfg() -> Config:
    return Config(
        _data={
            "long_trade_runner": {
                "enabled": True,
                "min_age_minutes": 3,
                "extended_target_r_multiple": 4.0,
                "widened_giveback_ratio": 0.40,
                "relaxed_lock_ratio": 0.65,
                "skip_max_age_on_profit": True,
            }
        }
    )


class LongTradeRunnerTests(unittest.TestCase):
    def test_runner_not_active_before_min_age(self) -> None:
        armed = time.time() - 60.0
        self.assertFalse(
            is_long_runner_active(
                armed_at=armed,
                peak_profit_gbp=2.0,
                trail_trigger_gbp=1.25,
                cfg=_cfg(),
            )
        )

    def test_extended_target_when_runner_active(self) -> None:
        armed = time.time() - 240.0
        tgt = effective_target_gbp(
            loss_cap_gbp=4.0,
            base_target_gbp=10.0,
            armed_at=armed,
            peak_profit_gbp=2.0,
            trail_trigger_gbp=1.25,
            cfg=_cfg(),
        )
        self.assertEqual(tgt, 16.0)

    def test_widened_giveback_when_runner_active(self) -> None:
        armed = time.time() - 240.0
        gb = effective_giveback_ratio(
            base_giveback=0.30,
            armed_at=armed,
            peak_profit_gbp=2.0,
            trail_trigger_gbp=1.25,
            cfg=_cfg(),
        )
        self.assertEqual(gb, 0.40)

    def test_skip_max_age_on_winning_long(self) -> None:
        self.assertTrue(
            skip_max_age_close_for_runner(
                side="BUY", entry=100.0, px=101.0, cfg=_cfg()
            )
        )
        self.assertFalse(
            skip_max_age_close_for_runner(
                side="BUY", entry=100.0, px=99.0, cfg=_cfg()
            )
        )

    def test_runner_engages_with_soak_trail_trigger_gbp(self) -> None:
        """micro_risk.trail_trigger_gbp=2.5 must still unlock 4R long-runner."""
        armed = time.time() - 240.0
        self.assertTrue(
            is_long_runner_active(
                armed_at=armed,
                peak_profit_gbp=2.5,
                trail_trigger_gbp=2.5,
                cfg=_cfg(),
            )
        )
        tgt = effective_target_gbp(
            loss_cap_gbp=4.0,
            base_target_gbp=6.0,
            armed_at=armed,
            peak_profit_gbp=2.5,
            trail_trigger_gbp=2.5,
            cfg=_cfg(),
        )
        self.assertEqual(tgt, 16.0)
