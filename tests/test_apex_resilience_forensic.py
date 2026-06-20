"""Forensic resilience checks — desktop watchdog skip and boot grace."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class WatchdogDesktopSkipTests(unittest.TestCase):
    def tearDown(self) -> None:
        from analytics.triage_logger import reset_triage_logger_for_tests
        from apex.microkernel import reset_microkernel_for_tests
        from trading.multi_api_broker import reset_multi_api_broker_for_tests

        reset_microkernel_for_tests()
        reset_multi_api_broker_for_tests()
        reset_triage_logger_for_tests()
        os.environ.pop("IG_API_PORT", None)
        os.environ.pop("IG_APEX_DESKTOP", None)
        os.environ.pop("IG_MULTI_API_BROKER", None)
        os.environ.pop("IG_TRIAGE_DB", None)

    def test_watchdog_skipped_on_desktop_port_9090(self) -> None:
        from apex.microkernel import get_microkernel, reset_microkernel_for_tests

        triage_tmp = tempfile.mkdtemp(prefix="forensic_triage_")
        self.addCleanup(lambda: __import__("shutil").rmtree(triage_tmp, ignore_errors=True))
        os.environ["IG_TRIAGE_DB"] = str(Path(triage_tmp) / "triage_v30.db")
        os.environ["IG_API_PORT"] = "9090"
        os.environ["IG_APEX_DESKTOP"] = "1"
        os.environ["IG_MULTI_API_BROKER"] = "0"

        reset_microkernel_for_tests()
        with patch("system.watchdog_sentinel.start_watchdog_self_healer") as start_wd:
            kernel = get_microkernel()
            kernel.start()
            start_wd.assert_not_called()
            kernel.stop()


class WatchdogSentinelForensicTests(unittest.TestCase):
    def test_ping_health_true_during_boot_grace(self) -> None:
        from system.watchdog_sentinel import WatchdogSelfHealer

        healer = WatchdogSelfHealer()
        healer._started_mono = __import__("time").monotonic()
        with patch("requests.get", side_effect=AssertionError("no network during grace")):
            self.assertTrue(healer._ping_health())

    def test_recovery_skips_socket_purge_when_ipc_bridge_running(self) -> None:
        from system.watchdog_sentinel import WatchdogSelfHealer

        healer = WatchdogSelfHealer()
        healer._started_mono = 0.0
        healer._last_recovery_mono = 0.0
        os.environ["IG_API_PORT"] = "9090"
        bridge = MagicMock()
        bridge.stats.return_value = {"running": True}
        with patch("apex.ipc_bridge.get_ipc_bridge", return_value=bridge):
            with patch("subprocess.run"):
                with patch("os.remove", side_effect=AssertionError("must not purge")):
                    with patch("subprocess.Popen"):
                        healer._execute_recovery()


class Gate2ShadowForensicTests(unittest.TestCase):
    def test_shadow_desktop_hydration_fast_path_in_source(self) -> None:
        src = (
            Path(__file__).resolve().parents[1] / "src" / "system" / "boot" / "gate2_runner.py"
        ).read_text(encoding="utf-8")
        self.assertIn("shadow desktop mock hydration", src)
        self.assertIn("is_shadow_node()", src)
        self.assertIn("IG_APEX_DESKTOP", src)
