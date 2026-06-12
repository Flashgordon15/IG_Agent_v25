"""Tests for progressive max_rsi scaling in ml_filter_overrides."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STRICT_MAX_RSI = 16.063651700824735


class ProgressiveMaxRsiScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        from system import ml_filter_overrides as mod

        self.mod = mod
        mod.reset_filter_overrides_cache_for_tests()

    def tearDown(self) -> None:
        from system import ml_filter_overrides as mod

        mod.reset_filter_overrides_cache_for_tests()

    def test_scale_boundary_and_nan_safe(self) -> None:
        scale = self.mod.scale_max_rsi
        baseline = self.mod.BASELINE_MAX_RSI
        strict = STRICT_MAX_RSI

        self.assertEqual(scale(strict, 0), baseline)
        self.assertEqual(scale(strict, 100), baseline)
        self.assertAlmostEqual(scale(strict, 500), strict, places=6)
        self.assertEqual(scale(float("nan"), 250), baseline)
        self.assertEqual(scale(strict, "bad"), baseline)

    def test_scale_at_record_counts(self) -> None:
        scale = self.mod.scale_max_rsi
        baseline = self.mod.BASELINE_MAX_RSI

        self.assertEqual(scale(STRICT_MAX_RSI, 0), baseline)
        self.assertEqual(scale(STRICT_MAX_RSI, 50), baseline)
        self.assertEqual(scale(STRICT_MAX_RSI, 100), baseline)
        self.assertAlmostEqual(scale(STRICT_MAX_RSI, 300), 43.03182414958763, places=4)
        self.assertAlmostEqual(scale(STRICT_MAX_RSI, 500), STRICT_MAX_RSI, places=6)
        self.assertAlmostEqual(scale(STRICT_MAX_RSI, 600), STRICT_MAX_RSI, places=6)

    def test_scale_clamps_when_strict_above_baseline_during_ramp(self) -> None:
        scale = self.mod.scale_max_rsi
        baseline = self.mod.BASELINE_MAX_RSI
        strict_high = 80.0

        self.assertEqual(scale(strict_high, 100), baseline)
        self.assertEqual(scale(strict_high, 300), baseline)
        self.assertEqual(scale(strict_high, 500), strict_high)

    def test_load_applies_progressive_max_rsi(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        meta = Path(tmp.name) / "meta.json"
        meta.write_text(
            json.dumps({"filter_overrides": {"max_rsi": STRICT_MAX_RSI}}),
            encoding="utf-8",
        )
        with patch.object(self.mod, "_meta_path", return_value=meta), patch.object(
            self.mod, "_overrides_enabled", return_value=True
        ), patch.object(self.mod, "training_record_count", return_value=300):
            bounds = self.mod.load_filter_overrides(force=True)
        self.assertAlmostEqual(bounds["max_rsi"], 43.03182414958763, places=4)

    def test_load_logs_once_on_init(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        meta = Path(tmp.name) / "meta.json"
        meta.write_text(
            json.dumps({"filter_overrides": {"max_rsi": STRICT_MAX_RSI}}),
            encoding="utf-8",
        )
        with patch.object(self.mod, "_meta_path", return_value=meta), patch.object(
            self.mod, "_overrides_enabled", return_value=True
        ), patch.object(self.mod, "training_record_count", return_value=14), patch(
            "system.ml_filter_overrides.log_engine"
        ) as mock_log:
            self.mod.load_filter_overrides(force=True)
            self.mod.load_filter_overrides(force=True)
        self.assertEqual(mock_log.call_count, 1)
        msg = mock_log.call_args[0][0]
        self.assertIn("ml_filter_overrides: max_rsi progressive scale", msg)
        self.assertIn("records=14", msg)
        self.assertIn("mode=baseline", msg)

    def test_evaluate_filter_uses_scaled_bound(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        meta = Path(tmp.name) / "meta.json"
        meta.write_text(
            json.dumps({"filter_overrides": {"max_rsi": STRICT_MAX_RSI}}),
            encoding="utf-8",
        )
        with patch.object(self.mod, "_meta_path", return_value=meta), patch.object(
            self.mod, "_overrides_enabled", return_value=True
        ), patch.object(self.mod, "training_record_count", return_value=14):
            blocked, reason = self.mod.evaluate_filter_block(
                adjusted_score=70,
                raw_score=70,
                rsi=20.0,
                atr_ratio=0.5,
            )
        self.assertFalse(blocked, reason)
        with patch.object(self.mod, "_meta_path", return_value=meta), patch.object(
            self.mod, "_overrides_enabled", return_value=True
        ), patch.object(self.mod, "training_record_count", return_value=500):
            self.mod.reset_filter_overrides_cache_for_tests()
            blocked, reason = self.mod.evaluate_filter_block(
                adjusted_score=70,
                raw_score=70,
                rsi=20.0,
                atr_ratio=0.5,
            )
        self.assertTrue(blocked)
        self.assertIn("max_rsi", reason)


if __name__ == "__main__":
    unittest.main()
