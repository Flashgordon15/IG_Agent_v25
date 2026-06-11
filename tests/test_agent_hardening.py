"""Tests for agent hardening — auto-start, health endpoint, watchdog."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.agent_control import (
    enrich_tick_runtime,
    register_trading_loop,
    reset_agent_control_for_tests,
    start_trading,
    stop_trading,
)
from api.close_handler import reset_close_handler_for_tests
from api.server import create_app
from api.snapshot_store import (
    reset_snapshot_store_for_tests,
    set_snapshot_path_for_tests,
)


def _reset_control() -> None:
    try:
        reset_agent_control_for_tests()
    except AttributeError:
        import api.agent_control as ac

        ac._loop = None  # type: ignore[attr-defined]
        ac._paused = False  # type: ignore[attr-defined]


class AutoStartTradingTests(unittest.TestCase):
    def test_main_registers_auto_start_via_start_trading(self) -> None:
        """Bootstrap must auto-start loops — user must not click Start."""
        source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        assert "register_api_startup(_start_live_engines)" in source
        assert "start_trading()" in source
        assert "Auto-start trading loops" in source

    def test_start_trading_clears_paused_and_starts_loop(self) -> None:
        _reset_control()
        loop = MagicMock()
        loop.is_running.return_value = False
        register_trading_loop(loop)
        stop_trading()
        result = start_trading()
        self.assertTrue(result["ok"])
        loop.start.assert_called_once()


class ApiHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        snap = Path(self.tmp.name) / "dashboard_snapshot.json"
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        _reset_control()
        set_snapshot_path_for_tests(snap)
        self.client = TestClient(create_app(watch_snapshot=False))

    def tearDown(self) -> None:
        self.client.close()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        _reset_control()
        self.tmp.cleanup()

    def test_api_health_endpoint_schema(self) -> None:
        loop = MagicMock()
        loop.is_running.return_value = True
        register_trading_loop(loop)

        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in (
            "ok",
            "agent_alive",
            "trading_healthy",
            "trading_loops_running",
            "trading_paused",
            "port_bound",
            "last_log_age_sec",
            "last_gate_check_age_sec",
            "watchdog_active",
            "quotes_fresh",
            "issues",
            "markets",
            "quote_fresh_by_epic",
            "supervision_drift_ok",
            "supervision_drift",
            "overnight_supervision",
        ):
            self.assertIn(key, body, f"missing /api/health field: {key}")
        self.assertIsInstance(body["agent_alive"], bool)
        self.assertTrue(body["trading_loops_running"])
        self.assertIsInstance(body["markets"], list)

    def test_state_includes_trading_loops_running(self) -> None:
        loop = MagicMock()
        loop.is_running.return_value = False
        register_trading_loop(loop)
        body = self.client.get("/state").json()
        self.assertIn("trading_loops_running", body)
        self.assertFalse(body["trading_loops_running"])

    def test_enrich_tick_runtime_adds_fields(self) -> None:
        loop = MagicMock()
        loop.is_running.return_value = True
        register_trading_loop(loop)
        enriched = enrich_tick_runtime({"type": "tick"})
        self.assertTrue(enriched["trading_loops_running"])
        self.assertFalse(enriched["trading_paused"])
        self.assertIn("supervision_drift_ok", enriched)
        self.assertIn("overnight_supervision", enriched)
        self.assertIn("watchdog_active", enriched)


class WatchdogDeploymentTests(unittest.TestCase):
    def test_watchdog_script_exists_and_executable(self) -> None:
        watchdog = ROOT / "scripts" / "watchdog.sh"
        self.assertTrue(watchdog.exists())
        self.assertTrue(os.access(watchdog, os.X_OK))

    def test_launcher_starts_watchdog(self) -> None:
        launch = ROOT / "launcher" / "templates" / "launch.sh"
        source = launch.read_text(encoding="utf-8")
        self.assertIn("ensure_watchdog", source)
        self.assertIn("scripts/watchdog.sh", source)


if __name__ == "__main__":
    unittest.main()
