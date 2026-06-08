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
    relaxation_enabled,
    reset_gate_relaxation_cache_for_tests,
)


class GateRelaxationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_gate_relaxation_cache_for_tests()

    def test_disabled_returns_default(self) -> None:
        with patch(
            "system.gate_relaxation._relaxation_block",
            return_value={"enabled": False},
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="HEALTHY"),
                55.0,
            )

    def test_indices_healthy_gets_52(self) -> None:
        with patch(
            "system.gate_relaxation._relaxation_block",
            return_value={
                "enabled": True,
                "fitness_min": 52,
                "epics": ["IX.D.DOW.IFM.IP"],
                "require_points_healthy": True,
            },
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="HEALTHY"),
                52.0,
            )

    def test_warning_keeps_55(self) -> None:
        with patch(
            "system.gate_relaxation._relaxation_block",
            return_value={
                "enabled": True,
                "fitness_min": 52,
                "epics": ["IX.D.DOW.IFM.IP"],
                "require_points_healthy": True,
            },
        ):
            self.assertEqual(
                effective_fitness_min("IX.D.DOW.IFM.IP", points_state="WARNING"),
                55.0,
            )

    def test_non_listed_epic_unchanged(self) -> None:
        with patch(
            "system.gate_relaxation._relaxation_block",
            return_value={
                "enabled": True,
                "fitness_min": 52,
                "epics": ["IX.D.DOW.IFM.IP"],
                "require_points_healthy": True,
            },
        ):
            self.assertEqual(
                effective_fitness_min("CS.D.CFPGOLD.CFP.IP", points_state="HEALTHY"),
                55.0,
            )

    def test_enabled_flag(self) -> None:
        with patch(
            "system.gate_relaxation._relaxation_block",
            return_value={"enabled": True},
        ):
            self.assertTrue(relaxation_enabled())


if __name__ == "__main__":
    unittest.main()
