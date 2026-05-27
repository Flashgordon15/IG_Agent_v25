"""Tests for dashboard API routes — Section 4.5 Step 13."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.server import create_app
from api.snapshot_store import reset_snapshot_store_for_tests, set_snapshot_path_for_tests


class DashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        snap = Path(self.tmp.name) / "snap.json"
        reset_snapshot_store_for_tests()
        set_snapshot_path_for_tests(snap)
        self.client = TestClient(create_app(watch_snapshot=False))

    def tearDown(self) -> None:
        self.client.close()
        reset_snapshot_store_for_tests()
        self.tmp.cleanup()

    def test_splash_dismiss(self) -> None:
        with patch("api.dashboard_data.version_json_path") as pmock:
            path = Path(self.tmp.name) / "version.json"
            path.write_text(json.dumps({"version": "25.1.0", "shown": False}), encoding="utf-8")
            pmock.return_value = path
            r = self.client.post("/api/splash/dismiss")
            self.assertEqual(r.status_code, 200)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["shown"])

    @patch("api.routes.start_trading", return_value={"ok": True, "status": "started"})
    def test_api_start(self, mock_start) -> None:
        r = self.client.post("/api/start")
        self.assertEqual(r.status_code, 200)
        mock_start.assert_called_once()

    @patch("api.routes.stop_trading", return_value={"ok": True, "status": "stopped"})
    def test_api_stop(self, mock_stop) -> None:
        r = self.client.post("/api/stop")
        self.assertEqual(r.status_code, 200)
        mock_stop.assert_called_once()

    def test_api_trades_and_signals(self) -> None:
        with patch("api.dashboard_data.get_closed_trades", return_value=[]):
            r = self.client.get("/api/trades")
            self.assertEqual(r.status_code, 200)
            self.assertIn("trades", r.json())
        with patch("api.dashboard_data.get_signal_log", return_value=[]):
            r = self.client.get("/api/signals")
            self.assertEqual(r.status_code, 200)
            self.assertIn("signals", r.json())


if __name__ == "__main__":
    unittest.main()
