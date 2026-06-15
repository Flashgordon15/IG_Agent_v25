"""Startup full test-suite gate."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class StartupTestSuiteTests(unittest.TestCase):
    def test_skips_in_pytest_context(self) -> None:
        from system.startup_test_suite import run_startup_test_suite

        with patch.dict(os.environ, {"IG_AGENT_PYTEST": "1"}, clear=False):
            result = run_startup_test_suite()
        self.assertTrue(result.ok)
        self.assertIn("skipped", result.note)

    def test_skips_on_watchdog_restart_env(self) -> None:
        from system.startup_test_suite import run_startup_test_suite

        with patch.dict(
            os.environ,
            {"IG_AGENT_PYTEST": "", "IG_AGENT_SKIP_DEPLOY_CHECK": "1"},
            clear=False,
        ):
            result = run_startup_test_suite()
        self.assertTrue(result.ok)
        self.assertIn("skipped", result.note)

    def test_failure_persists_report(self) -> None:
        from system import startup_test_suite as sts

        fake = MagicMock(returncode=1, stdout="1 failed, 10 passed in 1.0s\n", stderr="")
        with patch.object(sts.subprocess, "run", return_value=fake):
            with patch.dict(
                os.environ,
                {"IG_AGENT_PYTEST": "", "IG_AGENT_SKIP_DEPLOY_CHECK": ""},
                clear=False,
            ):
                with patch("system.telegram_notifier.send_critical_alert"):
                    result = sts.run_startup_test_suite()
        self.assertFalse(result.ok)
        report = sts.read_failure_report()
        self.assertIsNotNone(report)
        self.assertIn("failed", str(report.get("note", "")).lower())
        sts.clear_failure_report()

    def test_success_clears_failure_report(self) -> None:
        from system import startup_test_suite as sts

        sts._write_failure_report({"note": "old"})
        fake = MagicMock(returncode=0, stdout="12 passed in 0.5s\n", stderr="")
        with patch.object(sts.subprocess, "run", return_value=fake):
            with patch.dict(
                os.environ,
                {"IG_AGENT_PYTEST": "", "IG_AGENT_SKIP_DEPLOY_CHECK": ""},
                clear=False,
            ):
                result = sts.run_startup_test_suite()
        self.assertTrue(result.ok)
        self.assertIsNone(sts.read_failure_report())

    def test_test_suite_phase_registered_before_ready(self) -> None:
        from system.startup_tracker import PHASES

        ids = [p[0] for p in PHASES]
        self.assertIn("test_suite", ids)
        self.assertLess(ids.index("test_suite"), ids.index("ready"))


if __name__ == "__main__":
    unittest.main()
