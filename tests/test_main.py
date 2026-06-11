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
    @patch("main.acquire_instance_lock", return_value=(False, "duplicate Delete"))
    @patch("main.validate_config", return_value=(True, []))
    @patch("main.merge_credentials_for_validation")
    @patch("main.load_raw_config_dict")
    @patch("system.watchdog_banner.record_startup_failure")
    def test_instance_lock_duplicate_does_not_record_watchdog_failure(
        self,
        watchdog_mock: MagicMock,
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
        watchdog_mock.assert_not_called()

    @patch("main.bootstrap_credentials")
    @patch("main.acquire_instance_lock", return_value=(True, "ok"))
    @patch("main.validate_config", return_value=(True, []))
    @patch("main.merge_credentials_for_validation")
    @patch("main.load_raw_config_dict")
    @patch("system.watchdog_banner.record_startup_success")
    def test_preflight_success(
        self,
        watchdog_success_mock: MagicMock,
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
        watchdog_success_mock.assert_called_once()

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


class PortConflictTests(unittest.TestCase):
    def test_check_port_available_when_free(self) -> None:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertTrue(main_mod.check_port_available(port))

    def test_check_port_available_when_listening(self) -> None:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertFalse(main_mod.check_port_available(port))
        finally:
            s.close()

    @patch("main.release_instance_lock")
    @patch("main.check_port_available", return_value=False)
    @patch("main.run_preflight", return_value=main_mod.EXIT_OK)
    def test_port_in_use_prints_banner_exits_and_releases_lock(
        self,
        _preflight: MagicMock,
        port_mock: MagicMock,
        release_mock: MagicMock,
    ) -> None:
        runtime = main_mod.AgentRuntime()
        stderr_capture: list[str] = []

        class _Err:
            def write(self, msg: str) -> int:
                stderr_capture.append(msg)
                return len(msg)

            def flush(self) -> None:
                pass

        with patch("sys.stderr", _Err()):
            with self.assertRaises(SystemExit) as ctx:
                runtime.run()
        self.assertEqual(ctx.exception.code, 1)
        port_mock.assert_called_once_with(main_mod._API_PORT)
        release_mock.assert_called_once()
        combined = "".join(stderr_capture)
        self.assertIn("port 8080 is already in use", combined)
        self.assertIn("lsof -i :8080", combined)

    @patch("uvicorn.run")
    @patch("main.create_app")
    @patch("main.register_api_startup")
    @patch("main.register_trading_loop")
    @patch("runtime.agent_bootstrap.build_market_orchestrator")
    @patch("main._rest_client_if_ready", return_value=None)
    @patch("main.apply_config_defaults", side_effect=lambda d: d)
    @patch("main._load_config")
    @patch("main.check_port_available", return_value=True)
    @patch("main.run_preflight", return_value=main_mod.EXIT_OK)
    def test_port_free_continues_to_uvicorn(
        self,
        _preflight: MagicMock,
        port_mock: MagicMock,
        load_mock: MagicMock,
        _defaults: MagicMock,
        _rest: MagicMock,
        loop_mock: MagicMock,
        _reg_loop: MagicMock,
        _reg_startup: MagicMock,
        _app_mock: MagicMock,
        uvicorn_mock: MagicMock,
    ) -> None:
        cfg = MagicMock()
        cfg.as_dict.return_value = {"epic": "IX.D.NIKKEI.IFM.IP"}
        load_mock.return_value = cfg
        loop_mock.return_value = MagicMock()
        runtime = main_mod.AgentRuntime()
        code = runtime.run()
        self.assertEqual(code, main_mod.EXIT_OK)
        port_mock.assert_called_once_with(main_mod._API_PORT)
        uvicorn_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
