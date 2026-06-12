"""Dynamic rotation window and grace-period gate tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime.market_orchestrator import (  # noqa: E402
    ROTATION_GRACE_CYCLES,
    select_active_rotation_epics,
)
from trading.trading_loop import (  # noqa: E402
    NOT_IN_TOP_3_VOLATILITY_ROTATION,
    TradingLoop,
)


class SelectActiveRotationEpicsTests(unittest.TestCase):
    def test_default_top_three_when_fourth_outside_threshold(self) -> None:
        ranked = [
            ("EPIC_A", 100.0),
            ("EPIC_B", 90.0),
            ("EPIC_C", 80.0),
            ("EPIC_D", 60.0),
            ("EPIC_E", 50.0),
        ]
        active = select_active_rotation_epics(ranked)
        self.assertEqual(active, ["EPIC_A", "EPIC_B", "EPIC_C"])

    def test_expands_to_five_when_fourth_and_fifth_within_ten_pct(self) -> None:
        ranked = [
            ("EPIC_A", 100.0),
            ("EPIC_B", 95.0),
            ("EPIC_C", 80.0),
            ("EPIC_D", 76.0),
            ("EPIC_E", 72.0),
        ]
        active = select_active_rotation_epics(ranked)
        self.assertEqual(
            active,
            ["EPIC_A", "EPIC_B", "EPIC_C", "EPIC_D", "EPIC_E"],
        )

    def test_expands_to_four_when_only_fourth_within_threshold(self) -> None:
        ranked = [
            ("EPIC_A", 100.0),
            ("EPIC_B", 95.0),
            ("EPIC_C", 80.0),
            ("EPIC_D", 74.0),
            ("EPIC_E", 50.0),
        ]
        active = select_active_rotation_epics(ranked)
        self.assertEqual(active, ["EPIC_A", "EPIC_B", "EPIC_C", "EPIC_D"])

    def test_fewer_than_three_online_returns_all_ranked(self) -> None:
        ranked = [("EPIC_A", 50.0), ("EPIC_B", 40.0)]
        self.assertEqual(select_active_rotation_epics(ranked), ["EPIC_A", "EPIC_B"])


class RotationGracePeriodTests(unittest.TestCase):
    def _minimal_loop(self, epic: str = "EPIC_DROPPED") -> TradingLoop:
        config = MagicMock()
        config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "enforce_top3_rotation_filter": True,
                "rotation_grace_cycles": ROTATION_GRACE_CYCLES,
            }.get(key, default)
        )
        loop = TradingLoop.__new__(TradingLoop)
        loop._config = config
        loop._epic = epic
        loop._rotation_grace_remaining = ROTATION_GRACE_CYCLES
        return loop

    @patch("system.gate_relaxation.rotation_filter_bypassed", return_value=False)
    @patch(
        "runtime.market_orchestrator.MarketOrchestrator.get_global_active_epics",
        return_value=["EPIC_A", "EPIC_B", "EPIC_C"],
    )
    def test_grace_allows_three_cycles_before_mute(
        self, _active: MagicMock, _bypass: MagicMock
    ) -> None:
        loop = self._minimal_loop()
        for cycle in range(ROTATION_GRACE_CYCLES):
            result = loop._gate_active_rotation()
            self.assertTrue(
                result.passed,
                f"cycle {cycle + 1} should pass grace",
            )
            self.assertIn("rotation grace", result.detail)

        muted = loop._gate_active_rotation()
        self.assertFalse(muted.passed)
        self.assertEqual(muted.detail, NOT_IN_TOP_3_VOLATILITY_ROTATION)

    @patch("system.gate_relaxation.rotation_filter_bypassed", return_value=False)
    @patch(
        "runtime.market_orchestrator.MarketOrchestrator.get_global_active_epics",
    )
    def test_reentry_resets_grace(
        self, mock_active: MagicMock, _bypass: MagicMock
    ) -> None:
        loop = self._minimal_loop(epic="EPIC_A")
        mock_active.return_value = ["EPIC_A", "EPIC_B", "EPIC_C"]
        loop._gate_active_rotation()
        self.assertEqual(loop._rotation_grace_remaining, ROTATION_GRACE_CYCLES)

        mock_active.return_value = ["EPIC_B", "EPIC_C", "EPIC_D"]
        loop._gate_active_rotation()
        self.assertEqual(loop._rotation_grace_remaining, ROTATION_GRACE_CYCLES - 1)


class OrchestratorDynamicExpansionIntegration(unittest.TestCase):
    def setUp(self) -> None:
        from runtime import market_orchestrator as mo

        self._orch_ref_backup = mo._ORCHESTRATOR_REF

    def tearDown(self) -> None:
        from runtime import market_orchestrator as mo

        mo._ORCHESTRATOR_REF = self._orch_ref_backup

    @patch(
        "runtime.market_orchestrator.MarketOrchestrator._strategy_session_eligible",
        return_value=True,
    )
    def test_orchestrator_expands_to_five_under_tight_vol_cluster(
        self, _session: MagicMock
    ) -> None:
        from runtime.market_orchestrator import MarketOrchestrator, attach_snapshot_handlers

        cfg = MagicMock()
        cfg.as_dict.return_value = {}
        scores = {
            "EPIC_1": 100.0,
            "EPIC_2": 98.0,
            "EPIC_3": 80.0,
            "EPIC_4": 76.0,
            "EPIC_5": 73.0,
        }
        loops = []
        for epic, fitness in scores.items():
            loop = MagicMock()
            loop._epic = epic
            loop._market = epic
            loop._env = MagicMock()
            loop._env._last = SimpleNamespace(total=float(fitness))
            loop._env.get_factors.return_value = {
                "trend": max(0.01, fitness * 0.25),
                "spread": 15.0,
                "atr": 20.0,
                "session": 10.0,
            }
            loop._publish_snapshots = False
            loop._on_snapshot = None
            loops.append(loop)

        orch = MarketOrchestrator(
            cfg,
            loops,
            primary_epic="EPIC_1",
            enabled_epics=list(scores.keys()),
        )
        attach_snapshot_handlers(orch)
        for epic, fitness in scores.items():
            loop = next(lo for lo in orch.loops if lo._epic == epic)
            loop._env._last.total = fitness
        orch.refresh_active_epics()
        active = orch.get_active_epics()
        self.assertEqual(len(active), 5)
        self.assertEqual(active, list(scores.keys()))


if __name__ == "__main__":
    unittest.main()
