"""Tests for v26 gate relaxation config and fitness floor override."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.gate_relaxation import (
    effective_fitness_min,
    effective_trade_confidence_threshold,
    relaxation_enabled,
    reset_gate_relaxation_cache_for_tests,
)


class GateRelaxationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_gate_relaxation_cache_for_tests()

    def test_disabled_returns_default(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={"enabled": False},
            ),
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="HEALTHY"),
                55.0,
            )

    def test_indices_healthy_gets_52(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=False,
            ),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={
                    "enabled": True,
                    "fitness_min": 52,
                    "epics": ["IX.D.DOW.IFM.IP"],
                    "require_points_healthy": True,
                },
            ),
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="HEALTHY"),
                52.0,
            )

    def test_warning_keeps_55_when_require_healthy(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=False,
            ),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={
                    "enabled": True,
                    "fitness_min": 52,
                    "epics": ["IX.D.DOW.IFM.IP"],
                    "require_points_healthy": True,
                },
            ),
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="WARNING"),
                55.0,
            )

    def test_warning_gets_52_when_relax_all_states(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=False,
            ),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={
                    "enabled": True,
                    "fitness_min": 52,
                    "epics": ["IX.D.DOW.IFM.IP"],
                    "require_points_healthy": False,
                },
            ),
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="WARNING"),
                52.0,
            )

    def test_warning_uses_instrument_threshold(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=False,
            ),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={
                    "enabled": True,
                    "warning_use_instrument_threshold": True,
                },
            ),
        ):
            self.assertEqual(
                effective_trade_confidence_threshold(
                    92.0,
                    points_state="WARNING",
                    instrument_threshold=70.0,
                ),
                70.0,
            )

    def test_non_listed_epic_unchanged(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=False,
            ),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={
                    "enabled": True,
                    "fitness_min": 52,
                    "epics": ["IX.D.DOW.IFM.IP"],
                    "require_points_healthy": True,
                },
            ),
        ):
            self.assertEqual(
                effective_fitness_min("CS.D.CFPGOLD.CFP.IP", points_state="HEALTHY"),
                55.0,
            )

    def test_enabled_flag(self) -> None:
        with (
            patch("system.gate_relaxation.demo_soak_enabled", return_value=False),
            patch(
                "system.learning_demo_policy.v26_gate_relaxations_suppressed",
                return_value=False,
            ),
            patch(
                "system.gate_relaxation._relaxation_block",
                return_value={"enabled": True},
            ),
        ):
            self.assertTrue(relaxation_enabled())

    def test_soak_fitness_floor_profile_b(self) -> None:
        with patch(
            "system.gate_relaxation.demo_soak_enabled",
            return_value=True,
        ), patch(
            "system.gate_relaxation._soak_block",
            return_value={"enabled": True, "fitness_min": 50, "relax_all_epics": True},
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="HEALTHY"),
                50.0,
            )


if __name__ == "__main__":
    unittest.main()
