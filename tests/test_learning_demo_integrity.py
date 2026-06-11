"""Profile B learning demo — integrity and sovereign £ cap."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.economic_check import check_risk_cap, integrity_gate_sourced_required
from execution.risk_manager import RiskManager
from execution.types import normalize_gate_execution_params
from system.config import Config
from system.gate_relaxation import (
    relaxation_enabled,
    reset_gate_relaxation_cache_for_tests,
)
from system.learning_demo_policy import (
    reset_learning_demo_policy_cache_for_tests,
    v26_gate_relaxations_suppressed,
)


class EconomicCheckTests(unittest.TestCase):
    def test_wall_street_floor_exceeds_cap(self) -> None:
        cfg = Config(
            _data={
                "risk_cap_gbp": 150,
                "ig_point_value_gbp": 7.87,
            }
        )
        ok, risk, cap = check_risk_cap(
            size=0.2,
            stop_pts=150.0,
            cfg=cfg,
            confidence=90.0,
            risk_band_label="full",
        )
        self.assertFalse(ok)
        self.assertGreater(risk, cap)

    def test_gate_params_carry_risk_band(self) -> None:
        out = normalize_gate_execution_params(
            {
                "actual_size": 0.3,
                "stop_points": 80,
                "limit_points": 240,
                "risk_band": "probe",
                "risk_cap_gbp": 150,
            }
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.get("risk_band"), "probe")


class RiskManagerCapTests(unittest.TestCase):
    def test_rejects_oversize_wall_street(self) -> None:
        cfg = Config(
            _data={
                "trade_size": 0.3,
                "risk_cap_gbp": 150,
                "risk_points": 80,
                "ig_point_value_gbp": 7.87,
                "adaptive_min_trade_size": 0.01,
                "adaptive_max_trade_size": 50,
                "adaptive_min_risk_points": 30,
                "adaptive_max_risk_points": 150,
                "max_spread_points": 100,
                "max_spread": 100,
                "reward_multiple": 3.0,
                "stop_distance_points": 80,
                "max_daily_loss": 500,
            }
        )
        rm = RiskManager(cfg)
        result = rm.assess(
            direction="BUY",
            execution_params={"size": 9.0, "risk": 150.0, "spread": 5},
        )
        self.assertFalse(result.approved)
        self.assertIn("cap", result.reason.lower())


class LearningDemoPolicyTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_gate_relaxation_cache_for_tests()
        reset_learning_demo_policy_cache_for_tests()

    def test_v26_suppressed_when_learning_demo_on(self) -> None:
        with patch(
            "system.learning_demo_policy._policy_block",
            return_value={
                "enabled": True,
                "suppress_v26_gate_relaxations": True,
            },
        ):
            reset_learning_demo_policy_cache_for_tests()
            self.assertTrue(v26_gate_relaxations_suppressed())
        with patch(
            "system.gate_relaxation._relaxation_block",
            return_value={"enabled": True, "fitness_min": 52},
        ):
            reset_gate_relaxation_cache_for_tests()
            with patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=True,
            ):
                with patch(
                    "system.gate_relaxation.demo_soak_enabled",
                    return_value=False,
                ):
                    self.assertFalse(relaxation_enabled())

    def test_integrity_required_when_profile_b(self) -> None:
        with patch(
            "system.learning_demo_policy.learning_demo_integrity_enabled",
            return_value=True,
        ):
            self.assertTrue(integrity_gate_sourced_required())
