"""Target-Seeking Alpha Engine — unit tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intelligence.alpha_trail import AlphaOptimisedTrailEngine, AlphaTrailPosition
from intelligence.autopilot_scaling import effective_autopilot_max_per_epic
from intelligence.integration import apply_intelligence_pre_dispatch
from intelligence.target_engine import (
    TargetSeekingEngine,
    apply_target_execution_adjustments,
    apply_target_position_cap,
    initialize_target_engine,
    reset_target_engine_for_tests,
    risk_compression_factor,
)


class TargetEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_target_engine_for_tests()

    def tearDown(self) -> None:
        reset_target_engine_for_tests()

    def test_risk_compression_factor_curve(self) -> None:
        self.assertAlmostEqual(risk_compression_factor(0, 1000), 1.0)
        self.assertAlmostEqual(risk_compression_factor(500, 1000), 0.5)
        self.assertAlmostEqual(risk_compression_factor(1000, 1000), 0.1)
        self.assertAlmostEqual(risk_compression_factor(1500, 1000), 0.1)

    def test_capital_preservation_at_target(self) -> None:
        engine = TargetSeekingEngine(target_daily_gbp=1000.0)
        engine.enabled = True
        with patch.object(engine, "resolve_p_day_realised", return_value=1000.0):
            snap = engine.refresh()
        self.assertTrue(snap["capital_preservation"])
        self.assertAlmostEqual(snap["mission_progress_pct"], 100.0)

    def test_apply_target_position_cap_scales_excess(self) -> None:
        cfg = {
            "intelligence_layer": {
                "enabled": True,
                "target_engine": {"enabled": True, "target_daily_gbp": 1000.0},
            }
        }

        class _Cfg:
            def get(self, key, default=None):
                return cfg.get(key, default)

        with patch(
            "intelligence.target_engine.get_target_engine"
        ) as mock_get:
            te = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
            te.last_p_day = 500.0
            te.last_factor = 0.5
            te.capital_preservation = False
            te.refresh = lambda **_: te.snapshot()  # type: ignore[method-assign]
            mock_get.return_value = te
            cap, reason = apply_target_position_cap(5, 2, "ladder", cfg=_Cfg())
        self.assertEqual(cap, 3)
        self.assertIn("factor=0.50", reason)

    def test_apply_target_execution_blocks_at_target(self) -> None:
        cfg = {
            "intelligence_layer": {
                "enabled": True,
                "target_engine": {"enabled": True, "target_daily_gbp": 1000.0},
            }
        }

        class _Cfg:
            def get(self, key, default=None):
                return cfg.get(key, default)

        with patch(
            "intelligence.target_engine.get_target_engine"
        ) as mock_get:
            te = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
            te.last_p_day = 1000.0
            te.last_factor = 0.1
            te.capital_preservation = True
            te.refresh = lambda **_: te.snapshot()  # type: ignore[method-assign]
            mock_get.return_value = te
            merged, reject = apply_target_execution_adjustments(
                {"size": 1.0}, config=_Cfg()
            )
        self.assertIsNotNone(reject)
        self.assertIn("CAPITAL_PRESERVATION", reject or "")

    def test_apply_target_execution_scales_size(self) -> None:
        cfg = {
            "intelligence_layer": {
                "enabled": True,
                "target_engine": {"enabled": True, "target_daily_gbp": 1000.0},
            }
        }

        class _Cfg:
            def get(self, key, default=None):
                return cfg.get(key, default)

        with patch(
            "intelligence.target_engine.get_target_engine"
        ) as mock_get:
            te = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
            te.last_p_day = 750.0
            te.last_factor = 0.25
            te.capital_preservation = False
            te.refresh = lambda **_: te.snapshot()  # type: ignore[method-assign]
            mock_get.return_value = te
            merged, reject = apply_target_execution_adjustments(
                {"size": 2.0}, config=_Cfg()
            )
        self.assertIsNone(reject)
        self.assertAlmostEqual(float(merged["size"]), 0.5)

    def test_alpha_trail_capital_preservation_uses_1x_atr(self) -> None:
        engine = AlphaOptimisedTrailEngine()
        pos = AlphaTrailPosition(
            epic="IX.D.NASDAQ.IFM.IP",
            side="BUY",
            entry=100.0,
            stop=99.0,
            target=101.0,
            atr_pts=10.0,
        )
        v = engine.compute(
            pos,
            bid=100.5,
            offer=100.6,
            micro_regime="MOMENTUM_UP",
            capital_preservation=True,
        )
        self.assertAlmostEqual(v.atr_multiple, 1.0)
        self.assertIn("capital preservation", v.detail)

    def test_initialize_target_engine_seeds_balance(self) -> None:
        cfg_data = {
            "intelligence_layer": {
                "enabled": True,
                "target_engine": {
                    "enabled": True,
                    "target_daily_gbp": 1000.0,
                    "simulated_equity_gbp": 10000.0,
                },
            },
            "portfolio_envelope": {"account_balance_gbp": 10000},
        }

        class _Cfg:
            learning_db = "src/data/learning_db.sqlite3"

            def get(self, key, default=None):
                return cfg_data.get(key, default)

        rest = MagicMock()
        rest.maybe_refresh_account_summary.return_value = {"balance": 10000.0}
        with patch("data.learning_store.LearningStore") as mock_store_cls, patch.object(
            TargetSeekingEngine, "resolve_p_day_realised", return_value=0.0
        ):
            mock_store_cls.return_value = MagicMock()
            engine = initialize_target_engine(_Cfg(), rest)
        self.assertEqual(engine.session_start_balance, 10000.0)

    def test_integration_merges_target_block_after_intelligence(self) -> None:
        cfg = {
            "intelligence_layer": {
                "enabled": True,
                "target_engine": {"enabled": True, "target_daily_gbp": 1000.0},
            }
        }

        class _Cfg:
            def get(self, key, default=None):
                return cfg.get(key, default)

        signal = MagicMock(epic="IX.D.NASDAQ.IFM.IP")
        with patch(
            "intelligence.pipeline_bridge.get_intelligence_layer"
        ) as mock_layer, patch(
            "intelligence.target_engine.get_target_engine"
        ) as mock_te:
            layer = MagicMock()
            layer.execution_adjustments.return_value = {
                "intelligence_spread_blocked": False,
                "intelligence_throttle_factor": 0.0,
            }
            mock_layer.return_value = layer
            te = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
            te.last_p_day = 1000.0
            te.capital_preservation = True
            te.refresh = lambda **_: te.snapshot()  # type: ignore[method-assign]
            mock_te.return_value = te
            _merged, reject = apply_intelligence_pre_dispatch(
                signal, {"size": 1.0}, config=_Cfg()
            )
        self.assertIsNotNone(reject)


if __name__ == "__main__":
    unittest.main()
