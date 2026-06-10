"""Tests for supervision lifecycle hardening."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from system.shutdown_cleanup import agent_fully_stopped, stopped_verification_checks


class SupervisionLifecycleTests(unittest.TestCase):
    def test_agent_fully_stopped_allows_launchd_watchdog(self) -> None:
        fake_data = Path("/tmp/ig_agent_test_supervision/data")
        with (
            patch("system.shutdown_cleanup._list_main_py_pids", return_value=[]),
            patch("system.shutdown_cleanup._port_bound", return_value=False),
            patch("system.shutdown_cleanup.data_dir", return_value=fake_data),
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=True,
            ),
        ):
            ok, issues = agent_fully_stopped()
        self.assertTrue(ok, msg=f"unexpected issues: {issues}")
        self.assertEqual(issues, [])

    def test_agent_fully_stopped_requires_no_watchdog_without_launchd(self) -> None:
        fake_data = Path("/tmp/ig_agent_test_supervision/data")
        with (
            patch("system.shutdown_cleanup._list_main_py_pids", return_value=[]),
            patch("system.shutdown_cleanup._port_bound", return_value=False),
            patch("system.shutdown_cleanup.data_dir", return_value=fake_data),
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=False,
            ),
            patch(
                "system.shutdown_cleanup.subprocess.run",
                return_value=Mock(returncode=0, stdout="999\n"),
            ),
        ):
            ok, issues = agent_fully_stopped()
        self.assertFalse(ok)
        self.assertIn("watchdog.sh still running", issues)

    def test_stopped_checks_label_launchd_preserved(self) -> None:
        with patch(
            "system.overnight_supervision.launchd_watchdog_active",
            return_value=True,
        ):
            checks = stopped_verification_checks([])
        labels = [c["label"] for c in checks]
        self.assertIn("Launchd supervision preserved", labels)


class SupervisionMonitorTests(unittest.TestCase):
    def test_evaluate_flags_armed_without_launchd(self) -> None:
        from system.supervision_monitor import evaluate_supervision_drift

        with (
            patch(
                "system.overnight_supervision.read_overnight_armed",
                return_value={"armed": True},
            ),
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=False,
            ),
            patch(
                "system.overnight_supervision.overnight_supervision_summary",
                return_value={
                    "launchd_watchdog": False,
                    "overnight_armed": True,
                },
            ),
            patch(
                "system.overnight_supervision.agent_process_supervision_status",
                return_value=(True, "ok"),
            ),
            patch("system.shutdown_cleanup.manual_stop_active", return_value=False),
            patch("system.supervision_monitor._agent_listening", return_value=False),
            patch("api.agent_health._watchdog_active", return_value=False),
        ):
            drift = evaluate_supervision_drift()
        self.assertFalse(drift["ok"])
        self.assertIn("overnight_armed_but_launchd_watchdog_missing", drift["issues"])

    def test_stop_watchdog_preserves_launchd_by_default(self) -> None:
        from api import agent_health

        with patch("subprocess.run") as mock_run:
            agent_health.stop_watchdog()
        bootout_calls = [
            c for c in mock_run.call_args_list if c.args and "bootout" in str(c.args[0])
        ]
        self.assertEqual(bootout_calls, [])

    def test_stop_watchdog_can_unload_launchd(self) -> None:
        from api import agent_health

        with patch("subprocess.run") as mock_run:
            agent_health.stop_watchdog(preserve_launchd=False)
        bootout_calls = [
            c for c in mock_run.call_args_list if c.args and "bootout" in str(c.args[0])
        ]
        self.assertEqual(len(bootout_calls), 1)


class LaunchdKeepAliveRegressionTests(unittest.TestCase):
    def test_perform_shutdown_cleanup_preserves_launchd(self) -> None:
        from system.shutdown_cleanup import (
            perform_shutdown_cleanup,
            reset_shutdown_cleanup_for_tests,
        )

        reset_shutdown_cleanup_for_tests()
        with (
            patch("api.agent_control.stop_trading"),
            patch("runtime.agent_bootstrap.stop_market_stream"),
            patch("runtime.agent_bootstrap.stop_ig_position_sync"),
            patch("system.trading_health_monitor.stop_trading_health_monitor"),
            patch("system.telegram_notifier.stop_telegram_heartbeat"),
            patch("data.learning_store.LearningStore"),
            patch("system.ig_rest_session.shutdown_shared_ig_session"),
            patch(
                "system.shutdown_cleanup.kill_other_agent_processes", return_value=[]
            ),
            patch("system.instance_lock.release_instance_lock"),
            patch("main._force_cleanup_port"),
            patch("api.agent_health.stop_watchdog") as mock_stop,
        ):
            perform_shutdown_cleanup(source="dashboard", skip_port_cleanup=True)
        mock_stop.assert_called_once_with(preserve_launchd=True)

    def test_dashboard_shutdown_does_not_unload_launchd(self) -> None:
        from fastapi.testclient import TestClient

        from api.server import create_app

        client = TestClient(create_app(watch_snapshot=False))
        with (
            patch("api.routes.os._exit"),
            patch("system.shutdown_cleanup.spawn_post_shutdown_verifier"),
            patch("system.shutdown_cleanup.mark_manual_stop"),
            patch("system.shutdown_cleanup.perform_shutdown_cleanup"),
            patch("api.agent_health.stop_watchdog") as mock_stop,
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=True,
            ),
        ):
            r = client.post("/api/shutdown")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        mock_stop.assert_not_called()
        labels = [c.get("label") for c in body.get("cleanup_checks") or []]
        self.assertIn("Launchd supervision", labels)
        client.close()

    def test_shutdown_verify_accepts_launchd_watchdog_after_stop(self) -> None:
        from system.shutdown_cleanup import (
            agent_fully_stopped,
            stopped_verification_checks,
        )

        fake_data = Path("/tmp/ig_agent_test_supervision/data")
        with (
            patch("system.shutdown_cleanup._list_main_py_pids", return_value=[]),
            patch("system.shutdown_cleanup._port_bound", return_value=False),
            patch("system.shutdown_cleanup.data_dir", return_value=fake_data),
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=True,
            ),
        ):
            ok, issues = agent_fully_stopped()
            checks = stopped_verification_checks(issues)
        self.assertTrue(ok)
        preserved = [
            c for c in checks if "Launchd supervision preserved" in str(c.get("label"))
        ]
        self.assertTrue(preserved)
        self.assertTrue(preserved[0]["ok"])

    def test_shutdown_api_includes_supervision_snapshot(self) -> None:
        from fastapi.testclient import TestClient

        from api.server import create_app

        client = TestClient(create_app(watch_snapshot=False))
        fake_drift = {"ok": True, "issues": [], "warnings": []}
        fake_summary = {"launchd_watchdog": True, "overnight_armed": False}
        with (
            patch("api.routes.os._exit"),
            patch("system.shutdown_cleanup.spawn_post_shutdown_verifier"),
            patch("system.shutdown_cleanup.mark_manual_stop"),
            patch("system.shutdown_cleanup.perform_shutdown_cleanup"),
            patch(
                "system.supervision_monitor.evaluate_supervision_drift",
                return_value=fake_drift,
            ),
            patch(
                "system.overnight_supervision.overnight_supervision_summary",
                return_value=fake_summary,
            ),
            patch(
                "system.overnight_supervision.launchd_watchdog_active",
                return_value=True,
            ),
        ):
            r = client.post("/api/shutdown")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("supervision", body)
        self.assertTrue(body["supervision"]["supervision_drift_ok"])
        self.assertTrue(
            body["supervision"]["overnight_supervision"]["launchd_watchdog"]
        )
        client.close()


if __name__ == "__main__":
    unittest.main()
