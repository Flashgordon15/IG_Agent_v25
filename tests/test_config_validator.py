"""Tests for system.config_validator — Section 4.5 Step 10."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system import config_validator as cv


def _instruments() -> dict:
    return {
        "japan_225": {
            "enabled": True,
            "epic": "IX.D.NIKKEI.IFM.IP",
            "name": "Japan 225",
            "signal_threshold": 85,
            "max_spread_pts": 35,
            "best_session": "23:00-06:00 BST",
        },
        "eur_usd": {
            "enabled": False,
            "epic": "CS.D.EURUSD.CFD.IP",
            "name": "EUR/USD",
            "signal_threshold": 88,
            "max_spread_pts": 3,
            "best_session": "12:00-17:00 BST",
        },
    }


def _full_config() -> dict:
    return {
        "ig_username": "user",
        "ig_password": "secret",
        "ig_api_key": "key123",
        "ig_account_id": "Z6BAH4",
        "account_id": "Z6BAH4",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "signal_threshold": 85,
        "allow_live_trading": True,
        "trade_size": 1.0,
        "stop_distance_points": 90,
        "max_spread_points": 35,
        "max_daily_loss_gbp": 200.0,
        "max_open_positions": 1,
        "cooldown_seconds": 180,
        "instruments": _instruments(),
    }


class ConfigValidatorTests(unittest.TestCase):
    def test_all_critical_missing_fails(self) -> None:
        valid, messages = cv.validate_config({})
        self.assertFalse(valid)
        errors = [m for m in messages if m.startswith("ERROR:")]
        self.assertGreaterEqual(len(errors), len(cv.CRITICAL_KEYS))

    def test_partial_critical_missing_fails(self) -> None:
        cfg = _full_config()
        del cfg["ig_password"]
        del cfg["epic"]
        valid, messages = cv.validate_config(cfg)
        self.assertFalse(valid)
        joined = " ".join(messages)
        self.assertIn("ig_password", joined)
        self.assertIn("epic", joined)

    @patch("system.config_validator.log_engine")
    def test_optional_missing_logs_warnings(self, log_mock) -> None:
        cfg = _full_config()
        del cfg["signal_threshold"]
        del cfg["trade_size"]
        with patch.object(cv, "emergency_stop_lock_present", return_value=False):
            valid, messages = cv.validate_config(cfg)
        self.assertTrue(valid)
        warnings = [m for m in messages if m.startswith("WARNING:")]
        self.assertGreaterEqual(len(warnings), 2)
        log_mock.assert_called()

    def test_all_keys_present_passes(self) -> None:
        with patch.object(cv, "emergency_stop_lock_present", return_value=False):
            valid, messages = cv.validate_config(_full_config())
        self.assertTrue(valid)
        self.assertFalse(any(m.startswith("ERROR:") for m in messages))

    def test_config_aliases_for_critical_keys(self) -> None:
        cfg = {
            "username": "u",
            "password": "p",
            "api_key": "k",
            "account_id": "Z6BAH4",
            "epic": "IX.D.NIKKEI.IFM.IP",
            "instruments": _instruments(),
        }
        with patch.object(cv, "emergency_stop_lock_present", return_value=False):
            valid, _ = cv.validate_config(cfg)
        self.assertTrue(valid)

    def test_missing_instruments_block_fails(self) -> None:
        cfg = _full_config()
        del cfg["instruments"]
        valid, messages = cv.validate_config(cfg)
        self.assertFalse(valid)
        self.assertTrue(any("instruments" in m for m in messages))

    def test_no_enabled_instruments_fails(self) -> None:
        cfg = _full_config()
        cfg["instruments"] = {
            "japan_225": {
                "enabled": False,
                "epic": "IX.D.NIKKEI.IFM.IP",
                "name": "Japan 225",
            }
        }
        valid, messages = cv.validate_config(cfg)
        self.assertFalse(valid)
        self.assertTrue(any("no instruments enabled" in m for m in messages))

    def test_lock_file_blocks_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / cv.LOCK_FILENAME).write_text("", encoding="utf-8")
            with patch("system.config_validator.project_root", return_value=root):
                valid, messages = cv.validate_config(_full_config())
            self.assertFalse(valid)
            self.assertTrue(
                any("emergency_stop.lock" in m.lower() for m in messages)
            )

    def test_apply_config_defaults(self) -> None:
        merged = cv.apply_config_defaults({"epic": "X"})
        self.assertEqual(merged["signal_threshold"], 85)
        self.assertEqual(merged["max_open_positions"], 1)
        self.assertEqual(merged["max_positions_per_epic"], 1)
        self.assertEqual(merged["max_daily_loss"], -200.0)


if __name__ == "__main__":
    unittest.main()
