"""HARDENED_TESTBED firewall and loopback transport tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestbedFirewallTests(unittest.TestCase):
    def tearDown(self) -> None:
        from system.apex_runtime_mode import reset_apex_runtime_mode_for_tests
        from system.testbed_firewall import reset_testbed_firewall_for_tests

        reset_testbed_firewall_for_tests()
        reset_apex_runtime_mode_for_tests()
        for key in (
            "IG_APEX_RUNTIME_MODE",
            "IG_AGENT_DATA_DIR",
            "IG_LEARNING_DB",
            "IG_TRIAGE_DB",
            "IG_TESTBED_ROOT",
        ):
            os.environ.pop(key, None)

    def test_runtime_mode_enum_testbed(self) -> None:
        from system.apex_runtime_mode import ApexRuntimeMode, apply_runtime_mode_to_environ

        os.environ["IG_APEX_RUNTIME_MODE"] = "HARDENED_TESTBED"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["IG_TESTBED_ROOT"] = tmp
            mode = apply_runtime_mode_to_environ()
            self.assertEqual(mode, ApexRuntimeMode.HARDENED_TESTBED)
            from system.testbed_firewall import (
                is_testbed_firewall_active,
                testbed_ledger_path,
                testbed_state_path,
            )

            self.assertTrue(is_testbed_firewall_active())
            self.assertTrue(str(testbed_state_path()).endswith("testbed_state.json"))
            self.assertTrue(str(testbed_ledger_path()).endswith("testbed_ledger.db"))

    def test_production_db_access_panics_in_testbed(self) -> None:
        from system.testbed_firewall import (
            TestbedFirewallPanic,
            arm_testbed_firewall,
            guard_database_path,
        )

        os.environ["IG_TESTBED_PANIC_RAISE"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["IG_TESTBED_ROOT"] = tmp
            arm_testbed_firewall()
            with self.assertRaises(TestbedFirewallPanic):
                guard_database_path("/tmp/learning_db.sqlite3")
        os.environ.pop("IG_TESTBED_PANIC_RAISE", None)

    def test_learning_store_uses_testbed_ledger_only(self) -> None:
        from data.learning_store import LearningStore
        from system.testbed_firewall import arm_testbed_firewall, testbed_ledger_path

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["IG_TESTBED_ROOT"] = tmp
            arm_testbed_firewall()
            store = LearningStore(str(testbed_ledger_path()))
            store.connect()
            store.close()

    def test_streaming_factory_returns_loopback_in_testbed(self) -> None:
        from ig_api.streaming_factory import create_streaming_client
        from ig_api.testbed_loopback_transport import TestbedLoopbackTransport
        from system.apex_runtime_mode import apply_runtime_mode_to_environ

        os.environ["IG_APEX_RUNTIME_MODE"] = "HARDENED_TESTBED"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["IG_TESTBED_ROOT"] = tmp
            apply_runtime_mode_to_environ()
            client = create_streaming_client(
                credentials=object(),
                session=object(),
                rest_client=None,
            )
            self.assertIsInstance(client, TestbedLoopbackTransport)

    def test_loopback_registers_fill_against_replay_mid(self) -> None:
        from ig_api.testbed_loopback_transport import TestbedLoopbackTransport, inject_replay_tick
        from system.testbed_firewall import arm_testbed_firewall, testbed_ledger_path

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["IG_TESTBED_ROOT"] = tmp
            arm_testbed_firewall()
            transport = TestbedLoopbackTransport()
            transport.connect()
            transport._emit_tick("CS.D.CFPGOLD.CFP.IP", 2400.0, 2400.4)
            fill = transport.register_fill(
                epic="CS.D.CFPGOLD.CFP.IP", side="BUY", size=1.0
            )
            self.assertAlmostEqual(fill.price, 2400.2, places=1)
            import sqlite3

            with sqlite3.connect(str(testbed_ledger_path())) as conn:
                count = conn.execute("SELECT COUNT(*) FROM testbed_fills").fetchone()[0]
            self.assertEqual(int(count), 1)
            transport.disconnect()


if __name__ == "__main__":
    unittest.main()
