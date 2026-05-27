"""Tests for src/main.py startup sequence — Section 4.5 Step 12."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import main as main_mod
from tests.test_config_validator import _full_config


class MainStartupTests(unittest.TestCase):
    def test_lock_present_exits(self) -> None:
        with patch("main.emergency_stop_lock_present", return_value=True):
            code = main_mod.run_preflight()
        self.assertEqual(code, main_mod.EXIT_LOCK)

    @patch("main.load_raw_config_dict")
    def test_invalid_config_exits(self, load_mock: MagicMock) -> None:
        load_mock.return_value = {"epic": "X"}
        with patch("main.emergency_stop_lock_present", return_value=False):
            with patch("main.merge_credentials_for_validation", side_effect=lambda d: d):
                code = main_mod.run_preflight()
        self.assertEqual(code, main_mod.EXIT_CONFIG)

    @patch("main.bootstrap_credentials")
    @patch("main.acquire_instance_lock", return_value=(False, "duplicate"))
    @patch("main.validate_config", return_value=(True, []))
    @patch("main.merge_credentials_for_validation")
    @patch("main.load_raw_config_dict")
    def test_instance_lock_duplicate_exits(
        self,
        load_mock: MagicMock,
        merge_mock: MagicMock,
        _val_mock: MagicMock,
        _lock_mock: MagicMock,
        _boot_mock: MagicMock,
    ) -> None:
        load_mock.return_value = _full_config()
        merge_mock.side_effect = lambda d: d
        with patch("main.emergency_stop_lock_present", return_value=False):
            code = main_mod.run_preflight()
        self.assertEqual(code, main_mod.EXIT_INSTANCE)

    @patch("main.bootstrap_credentials")
    @patch("main.acquire_instance_lock", return_value=(True, "ok"))
    @patch("main.validate_config", return_value=(True, []))
    @patch("main.merge_credentials_for_validation")
    @patch("main.load_raw_config_dict")
    def test_preflight_success(
        self,
        load_mock: MagicMock,
        merge_mock: MagicMock,
        _val_mock: MagicMock,
        _lock_mock: MagicMock,
        boot_mock: MagicMock,
    ) -> None:
        load_mock.return_value = _full_config()
        merge_mock.side_effect = lambda d: d
        with patch("main.emergency_stop_lock_present", return_value=False):
            code = main_mod.run_preflight()
        self.assertEqual(code, main_mod.EXIT_OK)
        boot_mock.assert_called_once()

    @patch("main.log_engine")
    def test_merge_credentials_adds_ig_keys(self, _log: MagicMock) -> None:
        cfg = {"epic": "IX.D.NIKKEI.IFM.IP"}
        creds = MagicMock()
        creds.ig_username = "u"
        creds.ig_password = "p"
        creds.ig_api_key = "k"
        creds.ig_account_id = "Z6BAH4"
        status = MagicMock(credentials=creds)
        with patch("main.try_load_credentials", return_value=status):
            merged = main_mod.merge_credentials_for_validation(cfg)
        self.assertEqual(merged["ig_username"], "u")
        self.assertEqual(merged["account_id"], "Z6BAH4")


if __name__ == "__main__":
    unittest.main()
