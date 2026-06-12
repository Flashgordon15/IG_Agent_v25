"""Tests for shadow_analytics and milestone_notifications."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from system import milestone_notifications as milestones
from system import shadow_analytics as analytics
from system.ml_filter_overrides import reset_filter_overrides_cache_for_tests


class ShadowAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db))
        self.store.connect()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_compute_plane_metrics_drawdown(self) -> None:
        pnls = [10.0, -5.0, 8.0, -12.0, 20.0]
        m = analytics.compute_plane_metrics(pnls)
        self.assertEqual(m["trade_count"], 5)
        self.assertEqual(m["wins"], 3)
        self.assertEqual(m["losses"], 2)
        self.assertAlmostEqual(m["win_rate"], 0.6)
        self.assertGreater(m["average_drawdown_gbp"], 0)
        self.assertGreater(m["max_drawdown_gbp"], m["average_drawdown_gbp"])

    def test_shadow_vs_live_comparison(self) -> None:
        self.store.ingest_ig_closed_transaction(
            {
                "deal_reference": "SH1",
                "market": "GBP/USD",
                "epic": "CS.D.GBPUSD.CFD.IP",
                "side": "BUY",
                "entry": 1.25,
                "exit": 1.26,
                "size": 1,
                "pnl_points": 10,
                "ig_pnl_currency": 50.0,
                "result": "WIN",
                "setup_key": "IG|imported",
                "source": "ig_import",
            }
        )
        self.store.conn.execute(
            """
            INSERT INTO trades (
                opened_at, closed_at, market, epic, side, entry, exit, size,
                pnl_points, ig_pnl_currency, result, setup_key, source, dry_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-06-12 10:00:00",
                "2026-06-12 11:00:00",
                "GBP/USD",
                "CS.D.GBPUSD.CFD.IP",
                "BUY",
                1.25,
                1.24,
                1.0,
                -10.0,
                -40.0,
                "LOSS",
                "BUY|bear|us_afternoon",
                "strategy",
                0,
            ),
        )
        self.store.conn.commit()
        report = analytics.build_shadow_vs_live_comparison(self.store.conn)
        self.assertEqual(report["shadow"]["trade_count"], 1)
        self.assertEqual(report["live"]["trade_count"], 1)
        self.assertTrue(report["shadow"]["is_shadow"])
        self.assertFalse(report["live"]["is_shadow"])


class MilestoneNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        milestones.reset_milestone_state_for_tests()
        reset_filter_overrides_cache_for_tests()

    def tearDown(self) -> None:
        milestones.reset_milestone_state_for_tests()
        reset_filter_overrides_cache_for_tests()

    def test_format_milestone_message(self) -> None:
        with patch.object(milestones, "_strict_max_rsi_from_meta", return_value=16.06):
            msg = milestones.format_milestone_message(100)
        self.assertIn("100/500", msg)
        self.assertIn("max_rsi:", msg)

    def test_crossing_threshold_fires_once(self) -> None:
        sent: list[str] = []

        def fake_post(url: str, text: str) -> bool:
            sent.append(text)
            return True

        with patch.object(milestones, "_webhook_urls", return_value=["https://hooks.slack.com/x"]), patch.object(
            milestones, "_post_webhook", side_effect=fake_post
        ), patch.object(milestones, "_strict_max_rsi_from_meta", return_value=16.06), patch.object(
            milestones,
            "_dispatch_milestone_notify",
            side_effect=lambda threshold, count: milestones.notify_milestone(threshold, count),
        ):
            milestones.on_training_records_changed(99, 100)
            milestones.on_training_records_changed(100, 101)
        self.assertEqual(len(sent), 1)
        self.assertIn("100/500", sent[0])

    def test_milestone_status_block(self) -> None:
        with patch.object(milestones, "training_record_count", return_value=14):
            block = milestones.milestone_status_block()
        self.assertEqual(block["training_records"], 14)
        self.assertEqual(block["next_milestone"], 100)

    def test_shadow_vs_live_metrics_shape(self) -> None:
        analytics.reset_shadow_analytics_cache_for_tests()
        with patch.object(
            analytics,
            "build_shadow_vs_live_comparison",
            return_value={
                "shadow": {"win_rate": 0.2, "profit_factor": 0.3, "average_drawdown_gbp": 100},
                "live": {"win_rate": 0.6, "profit_factor": 0.5, "average_drawdown_gbp": 50},
                "comparison": {},
            },
        ):
            payload = analytics.shadow_vs_live_metrics(force=True)
        self.assertTrue(payload["ok"])
        self.assertIn("shadow", payload)
        self.assertIn("live", payload)
        self.assertEqual(payload["live"]["win_rate"], 0.6)


if __name__ == "__main__":
    unittest.main()
