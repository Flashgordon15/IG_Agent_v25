"""Tests for FastAPI server — Section 4.5 Step 8."""

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

from api.close_handler import register_close_handler, reset_close_handler_for_tests
from api.server import create_app
from api.snapshot import GATE_NAMES, build_default_tick
from api.snapshot_store import (
    get_tick,
    publish_tick,
    reset_snapshot_store_for_tests,
    set_snapshot_path_for_tests,
    subscribe,
)


class ApiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        snap = Path(self.tmp.name) / "dashboard_snapshot.json"
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        set_snapshot_path_for_tests(snap)
        self.client = TestClient(create_app(watch_snapshot=False))

    def tearDown(self) -> None:
        self.client.close()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        self.tmp.cleanup()

    def test_health_ok(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], "v29.1")

    def test_state_default_tick_schema(self) -> None:
        r = self.client.get("/state")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "tick")
        self.assertIn(body["market_state"], ("OPEN", "CLOSED", "STALE", "OFFLINE"))
        self.assertIn(
            body["stream_status"],
            ("LIVE", "STALE", "DISCONNECTED"),
        )
        self.assertEqual(len(body["health"]["gates"]), len(GATE_NAMES))
        for g in body["health"]["gates"]:
            self.assertIn(g["name"], GATE_NAMES)
        self.assertIn(body["health"]["badge"], ("WATCHING", "READY", "BLOCKED"))
        self.assertIn(body["signal"]["direction"], ("WAIT", "BUY", "SELL"))
        self.assertIn(
            body["points"]["state"],
            ("HEALTHY", "CAUTION", "WARNING", "STOP"),
        )
        self.assertIsInstance(body["positions"], list)

    def test_state_reflects_published_tick(self) -> None:
        tick = build_default_tick()
        tick["market_state"] = "OPEN"
        tick["bid"] = 65331.2
        tick["offer"] = 65338.2
        tick["spread"] = 7.0
        tick["stream_status"] = "LIVE"
        publish_tick(tick)
        body = self.client.get("/state").json()
        self.assertEqual(body["market_state"], "OPEN")
        self.assertEqual(body["bid"], 65331.2)

    def test_websocket_initial_tick(self) -> None:
        with self.client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            self.assertEqual(first["type"], "tick")
            self.assertIn("health", first)

    def test_subscribe_notifies_on_publish(self) -> None:
        seen: list[str] = []

        def _on_tick(t: dict) -> None:
            seen.append(str(t.get("market_state")))

        unsub = subscribe(_on_tick)
        try:
            tick = build_default_tick()
            tick["market_state"] = "OPEN"
            publish_tick(tick)
            self.assertEqual(seen, ["OPEN"])
        finally:
            unsub()

    def test_close_deal_uses_registered_handler(self) -> None:
        calls: list[str] = []

        def _fake(deal_id: str) -> dict:
            calls.append(deal_id)
            return {"verified_closed": True}

        register_close_handler(_fake)
        r = self.client.post("/api/close/DIAAAA123")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(calls, ["DIAAAA123"])

    def test_close_missing_deal_404(self) -> None:
        def _missing(_did: str) -> dict:
            raise LookupError("not found")

        register_close_handler(_missing)
        r = self.client.post("/api/close/UNKNOWN")
        self.assertEqual(r.status_code, 404)

    def test_shutdown_post_returns_ok_not_422(self) -> None:
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
        self.assertEqual(body.get("status"), "shutting_down")
        self.assertIn("supervision", body)

    def test_snapshot_persisted_to_disk(self) -> None:
        tick = build_default_tick()
        tick["daily_pnl_gbp"] = 42.5
        publish_tick(tick)
        path = Path(self.tmp.name) / "dashboard_snapshot.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["daily_pnl_gbp"], 42.5)
        self.assertEqual(get_tick()["daily_pnl_gbp"], 42.5)


if __name__ == "__main__":
    unittest.main()
