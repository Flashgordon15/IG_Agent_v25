"""Tests for trading.instrument_registry — Section 4.5 Step 11."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.instrument_registry import InstrumentRegistry

CONFIG_PATH = ROOT / "config" / "config_v25.json"
JAPAN_EPIC = "IX.D.NIKKEI.IFM.IP"


class InstrumentRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_get_enabled_japan_225_only(self) -> None:
        reg = InstrumentRegistry(self.config)
        enabled = reg.get_enabled()
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0]["name"], "Japan 225")
        self.assertEqual(enabled[0]["epic"], JAPAN_EPIC)
        self.assertTrue(enabled[0]["enabled"])

    def test_get_all_returns_four(self) -> None:
        reg = InstrumentRegistry(self.config)
        self.assertEqual(len(reg.get_all()), 4)

    def test_get_by_epic_finds_japan(self) -> None:
        reg = InstrumentRegistry(self.config)
        inst = reg.get_by_epic(JAPAN_EPIC)
        self.assertIsNotNone(inst)
        assert inst is not None
        self.assertEqual(inst["name"], "Japan 225")

    def test_get_by_epic_unknown_returns_none(self) -> None:
        reg = InstrumentRegistry(self.config)
        self.assertIsNone(reg.get_by_epic("UNKNOWN.EPIC"))


if __name__ == "__main__":
    unittest.main()
