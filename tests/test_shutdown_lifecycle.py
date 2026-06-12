"""Shutdown / supervision lifecycle — unit tests (no live processes)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class StopWatchdogLogicTests(unittest.TestCase):
    @patch("api.agent_health._WATCHDOG_PID_FILE")
    @patch("api.agent_health._standalone_watchdog_pids", return_value=[])
    @patch("system.overnight_supervision.launchd_watchdog_active", return_value=True)
    def test_preserve_when_launchd_loaded(
        self, _launchd: MagicMock, _pids: MagicMock, _pid_file: MagicMock
    ) -> None:
        from api.agent_health import stop_watchdog

        stop_watchdog(preserve_launchd=True)
        _pids.assert_not_called()

    @patch("api.agent_health._WATCHDOG_PID_FILE")
    @patch("api.agent_health._standalone_watchdog_pids", return_value=[])
    @patch("api.agent_health.subprocess.run")
    @patch("system.overnight_supervision.launchd_watchdog_active", return_value=False)
    def test_stops_standalone_when_no_launchd(
        self,
        _launchd: MagicMock,
        mock_run: MagicMock,
        _pids: MagicMock,
        pid_file: MagicMock,
    ) -> None:
        from api.agent_health import stop_watchdog

        pid_file.is_file.return_value = False
        pid_file.unlink = MagicMock()
        stop_watchdog(preserve_launchd=True)
        pid_file.unlink.assert_called_once()

    @patch("api.agent_health._WATCHDOG_PID_FILE")
    @patch("api.agent_health._standalone_watchdog_pids", return_value=[])
    @patch("api.agent_health.subprocess.run")
    @patch("system.overnight_supervision.launchd_watchdog_active", return_value=False)
    def test_always_unlinks_pid_even_if_pgrep_empty(
        self,
        _launchd: MagicMock,
        _mock_run: MagicMock,
        _pids: MagicMock,
        pid_file: MagicMock,
    ) -> None:
        from api.agent_health import stop_watchdog

        pid_file.is_file.return_value = True
        pid_file.read_text.return_value = "99999"
        pid_file.unlink = MagicMock()
        stop_watchdog(preserve_launchd=True)
        pid_file.unlink.assert_called_once()


class InstanceLockTests(unittest.TestCase):
    def test_force_release_clears_stale_lock(self) -> None:
        from system import instance_lock as il
        from system.paths import data_dir

        from system.instance_lock import lock_path

        lock = lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999999\n", encoding="utf-8")
        il.force_release_instance_lock()
        self.assertFalse(lock.exists())


class ShutdownVerificationTests(unittest.TestCase):
    @patch("system.shutdown_cleanup._list_main_py_pids", return_value=[])
    @patch("system.shutdown_cleanup._port_bound", return_value=False)
    @patch("system.overnight_supervision.launchd_watchdog_active", return_value=False)
    @patch("subprocess.run")
    def test_fully_stopped_passes_clean_state(
        self,
        mock_run: MagicMock,
        _launchd: MagicMock,
        _port: MagicMock,
        _pids: MagicMock,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        from system.paths import data_dir
        from system.shutdown_cleanup import agent_fully_stopped

        from system.instance_lock import lock_path

        lock = lock_path()
        wd_pid = data_dir() / "watchdog.pid"
        lock.unlink(missing_ok=True)
        wd_pid.unlink(missing_ok=True)
        ok, issues = agent_fully_stopped()
        self.assertTrue(ok, issues)

    def test_stopped_verification_check_labels(self) -> None:
        from system.shutdown_cleanup import stopped_verification_checks

        checks = stopped_verification_checks([])
        labels = [str(c["label"]) for c in checks]
        self.assertIn("No main.py process", labels)
        self.assertIn("Port 8080 free", labels)


if __name__ == "__main__":
    unittest.main()
