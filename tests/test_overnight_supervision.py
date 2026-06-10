"""Tests for overnight process supervision detection."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from system.overnight_supervision import (
    agent_process_supervision_status,
    clear_overnight_armed,
    ensure_launchd_supervision_loaded,
    launchd_supervision_status,
    mark_overnight_armed,
    overnight_supervision_summary,
    read_overnight_armed,
)


class OvernightSupervisionTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_overnight_armed()

    def test_launchd_not_installed_reports_fail(self) -> None:
        with patch(
            "system.overnight_supervision._launchd_job_loaded", return_value=False
        ):
            ok, detail = launchd_supervision_status()
        self.assertFalse(ok)
        self.assertIn("install_launchd", detail)

    def test_launchd_watchdog_passes(self) -> None:
        with patch(
            "system.overnight_supervision._launchd_job_loaded",
            side_effect=lambda label: label.endswith("watchdog"),
        ):
            ok, detail = launchd_supervision_status()
        self.assertTrue(ok)
        self.assertIn("watchdog", detail)

    def test_overnight_ok_requires_launchd(self) -> None:
        with (
            patch(
                "system.overnight_supervision.launchd_supervision_status",
                return_value=(False, "missing"),
            ),
            patch(
                "system.overnight_supervision.agent_process_supervision_status",
                return_value=(True, "detached"),
            ),
        ):
            summary = overnight_supervision_summary()
        self.assertFalse(summary["overnight_ok"])
        self.assertTrue(summary["agent_supervision_ok"])

    def test_cursor_ancestor_fails_without_launchd(self) -> None:
        with (
            patch(
                "system.overnight_supervision._launchd_job_loaded", return_value=False
            ),
            patch("system.overnight_supervision._listener_pid", return_value=4242),
            patch(
                "system.overnight_supervision._fragile_ancestors",
                return_value=[(100, "/Applications/Cursor.app/Contents/MacOS/Cursor")],
            ),
        ):
            ok, detail = agent_process_supervision_status()
        self.assertFalse(ok)
        self.assertIn("IDE/terminal", detail)

    def test_mark_and_clear_overnight_armed(self) -> None:
        mark_overnight_armed(source="test")
        armed = read_overnight_armed()
        self.assertTrue(armed.get("armed"))
        self.assertEqual(armed.get("source"), "test")
        clear_overnight_armed()
        self.assertFalse(read_overnight_armed().get("armed"))

    def test_ensure_bootstrap_when_plists_missing(self) -> None:
        with (
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=False,
            ),
            patch(
                "system.overnight_supervision._launch_agents_dir",
                return_value=Path("/nonexistent"),
            ),
        ):
            ok, detail = ensure_launchd_supervision_loaded()
        self.assertFalse(ok)
        self.assertIn("install_launchd", detail)


if __name__ == "__main__":
    unittest.main()
