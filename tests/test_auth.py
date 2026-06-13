"""Tests for admin auth middleware and login endpoint."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.auth import admin_password, reset_auth_for_tests
from api.close_handler import reset_close_handler_for_tests
from api.server import create_app
from api.snapshot_store import reset_snapshot_store_for_tests, set_snapshot_path_for_tests


def _login(client: TestClient, password: str | None = None) -> str:
    pwd = password if password is not None else admin_password()
    res = client.post("/api/auth/login", json={"password": pwd})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("authenticated") is True
    token = res.headers.get("X-Auth-Token") or res.cookies.get("ig_agent_auth")
    assert token
    return token


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_auth_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        snap = Path(self.tmp.name) / "dashboard_snapshot.json"
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        set_snapshot_path_for_tests(snap)
        self.client = TestClient(create_app(watch_snapshot=False))

    def tearDown(self) -> None:
        self.client.close()
        reset_auth_for_tests()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        self.tmp.cleanup()

    def test_login_success_and_failure(self) -> None:
        bad = self.client.post("/api/auth/login", json={"password": "wrong-password"})
        self.assertEqual(bad.status_code, 401)

        token = _login(self.client)
        self.assertTrue(token)

    def test_health_requires_auth(self) -> None:
        blocked = self.client.get("/api/health")
        self.assertEqual(blocked.status_code, 401)

        token = _login(self.client)
        ok = self.client.get("/api/health", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("agent_alive", ok.json())

    def test_admin_routes_require_auth(self) -> None:
        blocked = self.client.get("/api/admin/risk-status")
        self.assertEqual(blocked.status_code, 401)

        token = _login(self.client)
        ok = self.client.get(
            "/api/admin/risk-status",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertIn(ok.status_code, (200, 503))

    def test_startup_status_public_with_boot_metrics(self) -> None:
        res = self.client.get("/api/startup/status")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("boot_metrics", body)
        self.assertIn("phases", body)

    def test_admin_password_from_env(self) -> None:
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-secret-123"}, clear=False):
            from importlib import reload
            import api.auth as auth_mod

            reload(auth_mod)
            self.assertEqual(auth_mod.admin_password(), "test-secret-123")
            os.environ.pop("ADMIN_PASSWORD", None)
            reload(auth_mod)


if __name__ == "__main__":
    unittest.main()
