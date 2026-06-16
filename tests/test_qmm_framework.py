"""QMM framework unit tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from execution.adaptive_horizon import classify_execution_horizon
from system.qmm_process_supervisor import (
    clear_process_entry_block,
    process_entry_blocked,
    set_process_entry_block,
)
from trading.qmm_asset_selector import extract_qmm_epic_metrics, rank_qmm_epics


class QmmAssetSelectorTests(unittest.TestCase):
    def test_rank_qmm_epics_orders_by_score(self) -> None:
        loop = MagicMock()
        loop._env = MagicMock()
        loop._env.get_sentiment_factor.return_value = {"label": "bullish"}
        loop._market = "Test"
        loop._signal_engine = None
        loop._config = MagicMock(stop_distance_points=40.0)
        with patch("trading.qmm_asset_selector._hub_atr_delta", return_value=0.2):
            ranked = rank_qmm_epics(
                [
                    ("EPIC.A", loop, 20.0, 2.0),
                    ("EPIC.B", loop, 40.0, 1.0),
                ],
                force_refresh=True,
            )
        self.assertEqual(ranked[0][0], "EPIC.B")
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_extract_qmm_epic_metrics_in_memory(self) -> None:
        loop = MagicMock()
        loop._env = MagicMock()
        loop._env.get_sentiment_factor.return_value = {"label": "neutral"}
        loop._market = "FX"
        loop._signal_engine = None
        loop._config = MagicMock(stop_distance_points=30.0)
        with patch("trading.qmm_asset_selector._hub_atr_delta", return_value=0.1):
            m = extract_qmm_epic_metrics(
                "CS.D.EURUSD.CFD.IP",
                loop,
                trend_cleanliness=25.0,
                relative_spread_cost=1.5,
            )
        self.assertGreater(m.rank_score, 0.0)
        self.assertEqual(m.epic, "CS.D.EURUSD.CFD.IP")


class AdaptiveHorizonTests(unittest.TestCase):
    def test_scalp_horizon_for_tight_range(self) -> None:
        plan = classify_execution_horizon(
            {
                "atr_multiplier": 0.2,
                "spread": 0.5,
                "quote_age_s": 2.0,
                "session_score": 0.0,
            },
            stop_points=40.0,
        )
        self.assertEqual(plan.horizon, "scalp")
        self.assertLess(plan.trailing_distance_points, 30.0)

    def test_trend_horizon_for_breakout(self) -> None:
        plan = classify_execution_horizon(
            {
                "atr_multiplier": 1.1,
                "spread": 1.0,
                "quote_age_s": 3.0,
                "session_score": 5.0,
            },
            stop_points=40.0,
        )
        self.assertEqual(plan.horizon, "trend")
        self.assertTrue(plan.news_flow_sensitive)
        self.assertGreater(plan.trailing_distance_points, 30.0)


class QmmProcessSupervisorTests(unittest.TestCase):
    def test_process_entry_block(self) -> None:
        clear_process_entry_block()
        blocked, _ = process_entry_blocked()
        self.assertFalse(blocked)
        set_process_entry_block("SPREAD_ATR_CIRCUIT")
        blocked, reason = process_entry_blocked()
        self.assertTrue(blocked)
        self.assertIn("SPREAD", reason)
        clear_process_entry_block()


if __name__ == "__main__":
    unittest.main()
