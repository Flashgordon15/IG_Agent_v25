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
    sb_prefer_long_hold,
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
                "sb_prefer_long_hold": True,
                "skip_scalp_banks_for_sb": True,
                "sb_accounts": ["Z6BAH3"],
                "sb_engine_origins": ["MACRO_SENTINEL"],
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

    def test_sb_macro_sentinel_prefers_long_hold(self) -> None:
        """Z6BAH3 / MACRO_SENTINEL must not be short-circuited by CFD scalp banks."""
        self.assertTrue(
            sb_prefer_long_hold(
                _cfg(),
                account_id="Z6BAH3",
                product_type="SPREADBET",
                engine_origin="MACRO_SENTINEL",
            )
        )
        self.assertFalse(
            sb_prefer_long_hold(
                _cfg(),
                account_id="Z6BAH4",
                product_type="CFD",
                engine_origin="QUANT_SNIPER",
            )
        )

    def test_sb_long_runner_path_not_disabled_by_cfd_chop_flags(self) -> None:
        """CFD-only chop flags in config must leave SB long-runner enabled."""
        cfg = Config(
            _data={
                "dual_core": {
                    "cfd_block_mean_reversion": True,
                    "cfd_require_15m_trend_ml_obi": True,
                },
                "micro_risk": {
                    "streak_protection": {
                        "cfd_block_mean_reversion": True,
                    }
                },
                "long_trade_runner": {
                    "enabled": True,
                    "min_age_minutes": 3,
                    "extended_target_r_multiple": 4.0,
                    "widened_giveback_ratio": 0.40,
                    "sb_prefer_long_hold": True,
                    "skip_scalp_banks_for_sb": True,
                    "sb_accounts": ["Z6BAH3"],
                    "sb_engine_origins": ["MACRO_SENTINEL"],
                },
            }
        )
        from runtime.long_trade_runner import runner_enabled

        armed = time.time() - 240.0
        self.assertTrue(runner_enabled(cfg))
        self.assertTrue(
            is_long_runner_active(
                armed_at=armed,
                peak_profit_gbp=2.5,
                trail_trigger_gbp=2.5,
                cfg=cfg,
            )
        )
        self.assertTrue(
            sb_prefer_long_hold(
                cfg,
                account_id="Z6BAH3",
                engine_origin="MACRO_SENTINEL",
            )
        )
        gb = effective_giveback_ratio(
            base_giveback=0.22,
            armed_at=armed,
            peak_profit_gbp=2.5,
            trail_trigger_gbp=2.5,
            cfg=cfg,
        )
        self.assertEqual(gb, 0.40)
