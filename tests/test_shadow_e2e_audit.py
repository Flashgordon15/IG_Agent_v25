"""v30 shadow workspace E2E audit — port isolation, microkernel, mock routing."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np


class ShadowPortIsolationTests(unittest.TestCase):
    def tearDown(self) -> None:
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        for key in (
            "NODE_ENV",
            "IG_NODE_PROFILE",
            "IG_API_PORT",
            "IG_APEX_DESKTOP",
        ):
            os.environ.pop(key, None)

    def test_shadow_resolves_9090_only(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        from system.boot.preflight_helpers import resolve_api_port
        from system.node_profile import get_node_profile, reset_node_profile_for_tests

        reset_node_profile_for_tests()
        profile = get_node_profile(reload=True)
        self.assertEqual(profile.api_port, 9090)
        self.assertEqual(resolve_api_port(), 9090)

    def test_shadow_port_check_ignores_production_bind(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        from system.boot.preflight_helpers import check_port_available, resolve_api_port
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        port = resolve_api_port()
        self.assertEqual(port, 9090)
        result = check_port_available(port)
        self.assertIsInstance(result, bool)


class ShadowAuthBypassTests(unittest.TestCase):
    def tearDown(self) -> None:
        from api.auth import reset_auth_for_tests
        from system.node_profile import reset_node_profile_for_tests

        reset_auth_for_tests()
        reset_node_profile_for_tests()
        os.environ.pop("NODE_ENV", None)
        os.environ.pop("IG_APEX_DESKTOP", None)

    def test_apex_bypass_token_on_shadow(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        from api.auth import validate_token
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        self.assertTrue(validate_token("v30_unlocked_session_token"))

    def test_apex_bypass_rejected_on_production(self) -> None:
        os.environ["NODE_ENV"] = "production"
        from api.auth import validate_token
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        self.assertFalse(validate_token("v30_unlocked_session_token"))


class ShadowPortCleanupTests(unittest.TestCase):
    def tearDown(self) -> None:
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        os.environ.pop("NODE_ENV", None)
        os.environ.pop("IG_APEX_PROTECT_PRODUCTION_PORTS", None)

    def test_clear_port_8080_noop_under_shadow_protect(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        os.environ["IG_APEX_PROTECT_PRODUCTION_PORTS"] = "1"
        from cockpit.port_cleanup import clear_port_8080
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        with patch("subprocess.check_output") as lsof_mock:
            cleared = clear_port_8080(port=8080)
        self.assertEqual(cleared, [])
        lsof_mock.assert_not_called()


class MicroKernelSanityTests(unittest.TestCase):
    def tearDown(self) -> None:
        from apex.microkernel import reset_microkernel_for_tests

        reset_microkernel_for_tests()

    def test_workers_process_tick_and_ring_buffer(self) -> None:
        from apex.microkernel import get_microkernel, reset_microkernel_for_tests
        from signals.indicators import ML_VETO_FLOOR

        reset_microkernel_for_tests()
        kernel = get_microkernel()
        kernel.start()

        quote = MagicMock(bid=100.0, offer=100.2)
        for _ in range(20):
            kernel.on_tick_ingest("CS.D.EURUSD.CFD.IP", quote)

        deadline = 3.0
        import time

        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline:
            stats = kernel.stats()
            if stats.get("ingested", 0) >= 10 and stats.get("math_done", 0) >= 5:
                break
            time.sleep(0.05)

        stats = kernel.stats()
        self.assertGreaterEqual(stats["ingested"], 10)
        self.assertGreaterEqual(stats["math_done"], 5)
        self.assertGreaterEqual(ML_VETO_FLOOR, 0.450 - 0.001)

        ring = kernel._ring_for("CS.D.EURUSD.CFD.IP")
        close, _, _ = ring.ordered_views()
        self.assertEqual(close.dtype, np.float64)
        self.assertGreaterEqual(len(close), 10)

        kernel.stop()


class ShadowGuardRoutingTests(unittest.TestCase):
    def tearDown(self) -> None:
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        for key in ("NODE_ENV", "IG_TRIAGE_DB", "IG_AGENT_SHADOW_DESK"):
            os.environ.pop(key, None)

    def test_place_market_order_mock_shadow_entry(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "triage_v30.db"
            os.environ["IG_TRIAGE_DB"] = str(db_path)

            from ig_api.rest_client import IGRestClient

            client = IGRestClient(MagicMock())
            with patch.object(client, "ensure_session", MagicMock()):
                result = client.place_market_order(
                    epic="CS.D.EURUSD.CFD.IP",
                    direction="BUY",
                    size=1.0,
                    stop_distance=10.0,
                )

            self.assertEqual(result["status"], "MOCK_SHADOW_ENTRY")
            self.assertTrue(result.get("shadow"))
            self.assertEqual(result["dealReference"], "MOCK_SHADOW_ENTRY")

            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT token, epic, direction FROM shadow_orders LIMIT 1"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "MOCK_SHADOW_ENTRY")
            self.assertEqual(row[1], "CS.D.EURUSD.CFD.IP")
            self.assertEqual(row[2], "BUY")


if __name__ == "__main__":
    unittest.main()
