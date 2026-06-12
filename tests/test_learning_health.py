"""Tests for learning health and protective learning helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class LearningTradePolicyTests(unittest.TestCase):
    def test_ig_import_setup_keys(self) -> None:
        from system.learning_trade_policy import is_ig_import_setup_key

        self.assertTrue(is_ig_import_setup_key("IG|imported"))
        self.assertTrue(is_ig_import_setup_key("IG_IMPORT"))
        self.assertFalse(is_ig_import_setup_key("BUY|bull|london|atr0-30"))


class MLFilterOverridesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        from system import ml_filter_overrides as mod

        self.mod = mod
        mod.reset_filter_overrides_cache_for_tests()

    def tearDown(self) -> None:
        from system import ml_filter_overrides as mod

        mod.reset_filter_overrides_cache_for_tests()
        self.tmp.cleanup()

    def test_max_rsi_blocks(self) -> None:
        meta = Path(self.tmp.name) / "meta.json"
        meta.write_text(
            json.dumps({"filter_overrides": {"max_rsi": 16.0}}),
            encoding="utf-8",
        )
        from unittest.mock import patch

        with patch.object(self.mod, "_meta_path", return_value=meta):
            blocked, reason = self.mod.evaluate_filter_block(
                adjusted_score=70,
                raw_score=70,
                rsi=20.0,
                atr_ratio=0.5,
            )
        self.assertTrue(blocked)
        self.assertIn("max_rsi", reason)


class SetupRegistryRefreshTests(unittest.TestCase):
    def test_bans_zero_winrate_setup(self) -> None:
        from system.setup_registry_refresh import _classify_status

        self.assertEqual(_classify_status(5, 0.0, -1.0, 5), "BANNED")
        self.assertEqual(_classify_status(3, 0.0, -1.0, 3), "INSUFFICIENT")


if __name__ == "__main__":
    unittest.main()
