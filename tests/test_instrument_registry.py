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

    def test_get_enabled_includes_japan_225(self) -> None:
        reg = InstrumentRegistry(self.config)
        enabled = reg.get_enabled()
        epics = {str(inst.get("epic") or "") for inst in enabled}
        self.assertIn(JAPAN_EPIC, epics)
        self.assertTrue(all(inst.get("enabled") for inst in enabled))

    def test_get_all_returns_at_least_four(self) -> None:
        reg = InstrumentRegistry(self.config)
        self.assertGreaterEqual(len(reg.get_all()), 4)

    def test_get_by_epic_finds_japan(self) -> None:
        reg = InstrumentRegistry(self.config)
        inst = reg.get_by_epic(JAPAN_EPIC)
        self.assertIsNotNone(inst)
        assert inst is not None
        self.assertEqual(inst["name"], "Japan 225")

    def test_get_by_epic_unknown_returns_none(self) -> None:
        reg = InstrumentRegistry(self.config)
        self.assertIsNone(reg.get_by_epic("UNKNOWN.EPIC"))

    def test_enabled_sorted_by_execution_priority_desc(self) -> None:
        cfg = {
            "instruments": {
                "low": {"enabled": True, "epic": "A", "execution_priority": 10},
                "high": {"enabled": True, "epic": "B", "execution_priority": 100},
                "mid": {"enabled": True, "epic": "C", "execution_priority": 50},
                "off": {"enabled": False, "epic": "D", "execution_priority": 999},
            }
        }
        reg = InstrumentRegistry(cfg)
        epics = [inst["epic"] for _iid, inst in reg.get_enabled_with_ids()]
        self.assertEqual(epics, ["B", "C", "A"])


if __name__ == "__main__":
    unittest.main()
