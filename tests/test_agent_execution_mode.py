"""Tests for IG_AGENT_MODE execution plane selector."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from system.agent_execution_mode import (
    agent_execution_mode,
    broker_demo_execution_required,
    demo_broker_execution_active,
    resolve_default_execution_mode_for_boot,
    shadow_execution_active,
)
from system.protective_learning import ensure_autonomous_engine_on_boot


class AgentExecutionModeTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in ("IG_AGENT_MODE", "IG_APEX_DESKTOP", "IG_MOCK_FEED"):
            os.environ.pop(key, None)

    def test_demo_mode_disables_shadow_router(self) -> None:
        with patch.dict(os.environ, {"IG_AGENT_MODE": "DEMO"}, clear=False):
            self.assertTrue(demo_broker_execution_active())
            self.assertFalse(shadow_execution_active())

    def test_shadow_mode_only_when_explicit(self) -> None:
        with patch.dict(os.environ, {"IG_AGENT_MODE": "SHADOW"}, clear=False):
            self.assertTrue(shadow_execution_active())
            self.assertFalse(demo_broker_execution_active())

    def test_mock_feed_zero_requires_broker_demo(self) -> None:
        with patch.dict(os.environ, {"IG_MOCK_FEED": "0"}, clear=False):
            self.assertTrue(broker_demo_execution_required())

    def test_ensure_autonomous_engine_never_forces_shadow(self) -> None:
        import system.protective_learning as pl

        pl._autonomous_engine_boot_armed = False
        with patch.dict(os.environ, {"IG_APEX_DESKTOP": "1"}, clear=False):
            ensure_autonomous_engine_on_boot()
            self.assertEqual(agent_execution_mode(), "DEMO")
            self.assertNotEqual(os.environ.get("IG_AGENT_MODE"), "SHADOW")


if __name__ == "__main__":
    unittest.main()
