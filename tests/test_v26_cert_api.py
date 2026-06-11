"""Tests for v26 CERT API."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.close_handler import reset_close_handler_for_tests
from api.server import create_app
from api.snapshot_store import reset_snapshot_store_for_tests
from api.v26_cert import build_cert_payload


class V26CertApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        self.client = TestClient(create_app(watch_snapshot=False))

    def tearDown(self) -> None:
        self.client.close()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()

    def test_build_cert_payload_has_levels(self) -> None:
        payload = build_cert_payload()
        self.assertIn("levels", payload)
        self.assertGreaterEqual(len(payload["levels"]), 4)

    def test_api_v26_cert_endpoint(self) -> None:
        r = self.client.get("/api/v26/cert")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("levels", body)


if __name__ == "__main__":
    unittest.main()
