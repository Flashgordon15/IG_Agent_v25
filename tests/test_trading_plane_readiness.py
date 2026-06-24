"""Trading plane readiness — skeleton vs live orchestrator detection."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from api.agent_control import register_trading_loop, reset_agent_control_for_tests
from system.trading_plane_readiness import (
    describe_trading_plane,
    is_trading_plane_live,
    reset_trading_plane_readiness_for_tests,
)


class TradingPlaneReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_agent_control_for_tests()
        reset_trading_plane_readiness_for_tests()

    def test_skeleton_orchestrator_not_live(self) -> None:
        orch = MagicMock()
        orch.is_running.return_value = False
        orch.loops = [MagicMock(_skeleton=True, is_running=MagicMock(return_value=False))]
        orch._v6_skeleton_mode = True
        orch._v6_materialized = False
        register_trading_loop(orch)

        status = describe_trading_plane()
        self.assertFalse(status["live"])
        self.assertIn("v6_skeleton_not_materialized", status["blockers"])
        self.assertFalse(is_trading_plane_live())

    def test_materialized_running_orchestrator_is_live(self) -> None:
        loop = MagicMock()
        loop._skeleton = False
        loop.is_running.return_value = True
        orch = MagicMock()
        orch.is_running.return_value = True
        orch.loops = [loop]
        orch._v6_skeleton_mode = False
        orch._v6_materialized = True
        register_trading_loop(orch)

        status = describe_trading_plane()
        self.assertTrue(status["live"])
        self.assertTrue(is_trading_plane_live())


if __name__ == "__main__":
    unittest.main()
