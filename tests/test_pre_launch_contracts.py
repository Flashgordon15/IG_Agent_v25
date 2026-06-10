"""Tier-1 pre-launch contracts — lifecycle and dashboard-critical API surface."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


class PreLaunchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "src"))
        from api.close_handler import reset_close_handler_for_tests
        from api.snapshot_store import (
            reset_snapshot_store_for_tests,
            set_snapshot_path_for_tests,
        )

        self.tmp = tempfile.TemporaryDirectory()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        set_snapshot_path_for_tests(Path(self.tmp.name) / "dashboard_snapshot.json")
        from api.server import create_app

        self.client = TestClient(create_app(watch_snapshot=False))

    def tearDown(self) -> None:
        self.client.close()
        import sys

        sys.path.insert(0, str(ROOT / "src"))
        from api.close_handler import reset_close_handler_for_tests
        from api.snapshot_store import reset_snapshot_store_for_tests

        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        self.tmp.cleanup()

    def test_critical_routes_respond_without_422(self) -> None:
        routes = [
            ("GET", "/api/health"),
            ("GET", "/state"),
            ("GET", "/api/startup/status"),
            ("GET", "/api/shutdown/verify-status"),
            ("POST", "/api/heartbeat"),
        ]
        for method, path in routes:
            with self.subTest(path=path):
                if method == "GET":
                    r = self.client.get(path)
                else:
                    r = self.client.post(path)
                self.assertNotEqual(
                    r.status_code,
                    422,
                    f"{method} {path} must not return FastAPI validation error",
                )

    def test_shutdown_verify_status_returns_structured_json(self) -> None:
        r = self.client.get("/api/shutdown/verify-status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("status", body)
        self.assertIn("checks", body)
        self.assertIsInstance(body["checks"], list)

    def test_shutdown_post_not_422(self) -> None:
        with (
            patch("api.routes.os._exit"),
            patch("system.shutdown_cleanup.spawn_post_shutdown_verifier"),
            patch("system.shutdown_cleanup.mark_manual_stop"),
            patch("system.shutdown_cleanup.perform_shutdown_cleanup"),
        ):
            r = self.client.post("/api/shutdown")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("supervision", body)
        cleanup_labels = [c.get("label") for c in body.get("cleanup_checks") or []]
        self.assertIn("Launchd supervision", cleanup_labels)

    def test_main_clears_manual_stop_on_startup(self) -> None:
        main_src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("clear_manual_stop()", main_src)
        self.assertIn("_pre_startup_cleanup", main_src)

    def test_feeder_bar_contract_module_exists(self) -> None:
        """bar_close shadow path must tolerate pandas Series snapshots."""
        loop_src = (ROOT / "src" / "trading" / "trading_loop.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_feeder_bar_from_snapshot", loop_src)
        self.assertIn("bar_close", loop_src)


class ManualStopLifecycleTests(unittest.TestCase):
    def test_mark_and_clear_manual_stop_roundtrip(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "src"))
        from system.paths import data_dir
        from system.shutdown_cleanup import (
            clear_manual_stop,
            manual_stop_active,
            mark_manual_stop,
        )

        flag = data_dir() / "state" / "manual_stop.json"
        try:
            mark_manual_stop(source="test")
            self.assertTrue(flag.is_file())
            self.assertTrue(manual_stop_active())
            clear_manual_stop()
            self.assertFalse(flag.is_file())
            self.assertFalse(manual_stop_active())
        finally:
            clear_manual_stop()


if __name__ == "__main__":
    unittest.main()
