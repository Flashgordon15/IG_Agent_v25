"""Tests for v26 ml_veto gate (disabled by default)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.trading_loop import TradingLoop


def _minimal_loop() -> TradingLoop:
    loop = TradingLoop.__new__(TradingLoop)
    loop._epic = "IX.D.NIKKEI.IFM.IP"
    loop._last_ml_prob = 0.42
    loop._last_sig_direction = "BUY"
    return loop


class MlVetoGateTests(unittest.TestCase):
    def test_ml_veto_off_by_default(self) -> None:
        loop = _minimal_loop()
        with (
            patch("system.gate_relaxation.soak_ml_veto_bypassed", return_value=False),
            patch(
                "system.v26_config.ml_veto_settings", return_value={"enabled": False}
            ),
        ):
            gate = loop._gate_ml_veto()
        self.assertTrue(gate.passed)
        self.assertEqual(gate.value, "off")

    def test_ml_veto_blocks_low_probability(self) -> None:
        loop = _minimal_loop()
        settings = {
            "enabled": True,
            "min_probability": 0.58,
            "use_s4_models": False,
            "per_epic": {},
        }
        with (
            patch("system.gate_relaxation.soak_ml_veto_bypassed", return_value=False),
            patch("system.v26_config.ml_veto_settings", return_value=settings),
            patch("system.v26_config.epic_ml_veto_enabled", return_value=True),
            patch("system.v26_config.epic_min_probability", return_value=0.58),
        ):
            gate = loop._gate_ml_veto()
        self.assertFalse(gate.passed)
        self.assertIn("veto", gate.detail)

    def test_ml_veto_passes_high_probability(self) -> None:
        loop = _minimal_loop()
        loop._last_ml_prob = 0.72
        settings = {
            "enabled": True,
            "min_probability": 0.58,
            "use_s4_models": False,
            "per_epic": {},
        }
        with (
            patch("system.v26_config.ml_veto_settings", return_value=settings),
            patch("system.v26_config.epic_ml_veto_enabled", return_value=True),
            patch("system.v26_config.epic_min_probability", return_value=0.58),
        ):
            gate = loop._gate_ml_veto()
        self.assertTrue(gate.passed)

    def test_gate_names_include_ml_veto(self) -> None:
        from api.snapshot import GATE_NAMES

        self.assertIn("ml_veto", GATE_NAMES)
        self.assertLess(
            GATE_NAMES.index("ml_veto"),
            GATE_NAMES.index("execution"),
        )

    def test_epic_whitelist_only_gbp_when_per_epic_set(self) -> None:
        from system.v26_config import epic_ml_veto_enabled

        settings = {
            "enabled": True,
            "per_epic": {
                "CS.D.GBPUSD.CFD.IP": {"enabled": True, "min_probability": 0.52}
            },
        }
        with patch("system.v26_config.ml_veto_settings", return_value=settings):
            self.assertTrue(epic_ml_veto_enabled("CS.D.GBPUSD.CFD.IP"))
            self.assertFalse(epic_ml_veto_enabled("IX.D.NIKKEI.IFM.IP"))

    def test_config_v26_ml_veto_structure(self) -> None:
        from system.v26_config import ml_veto_settings, reset_v26_config_cache_for_tests

        reset_v26_config_cache_for_tests()
        cfg = ml_veto_settings()
        self.assertIn("per_epic", cfg)
        self.assertIsInstance(cfg.get("per_epic"), dict)
        self.assertTrue(cfg.get("enabled"))


if __name__ == "__main__":
    unittest.main()
