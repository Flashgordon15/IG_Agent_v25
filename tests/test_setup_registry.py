"""Setup registry + expectancy gate helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SetupRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        from system import setup_registry as sr

        self.sr = sr
        sr.reset_registry_cache_for_tests()

    def tearDown(self) -> None:
        from system import setup_registry as sr

        sr.reset_registry_cache_for_tests()
        self.tmp.cleanup()

    def test_banned_setup_blocked_when_enabled(self) -> None:
        path = Path(self.tmp.name) / "setup_registry.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "enabled": True,
                    "setups": {
                        "SELL|bear|asia|atr": {
                            "status": "BANNED",
                            "n": 25,
                            "wr": 0.4,
                            "e_gbp": -12.0,
                            "total_pnl_gbp": -300,
                        }
                    },
                    "banned_keys": ["SELL|bear|asia|atr"],
                }
            ),
            encoding="utf-8",
        )
        from unittest.mock import patch

        with patch.object(self.sr, "registry_path", return_value=path):
            self.assertTrue(self.sr.is_gate_enabled())
            self.assertTrue(self.sr.is_setup_banned("SELL|bear|asia|atr"))
            self.assertFalse(self.sr.is_setup_banned("BUY|bull|london|atr"))

    def test_inactive_registry_passes_all(self) -> None:
        path = Path(self.tmp.name) / "setup_registry.json"
        path.write_text(
            json.dumps(
                {"version": 1, "enabled": False, "setups": {}, "banned_keys": []}
            ),
            encoding="utf-8",
        )
        from unittest.mock import patch

        with patch.object(self.sr, "registry_path", return_value=path):
            self.assertFalse(self.sr.is_gate_enabled())
            self.assertFalse(self.sr.is_setup_banned("ANY|setup"))


if __name__ == "__main__":
    unittest.main()
