"""v30 Apex triage logger — schema, analytics, slippage, Worker D hooks."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analytics.triage_logger import (  # noqa: E402
    ClosedPositionRecord,
    LatencyMetricRecord,
    SessionPerformanceTracker,
    TriageLogger,
    analyze_broker_fill_slippage,
    quantify_slippage,
    reset_triage_logger_for_tests,
    resolve_triage_db_path,
)
from apex.microkernel import reset_microkernel_for_tests  # noqa: E402
from system.node_profile import reset_node_profile_for_tests  # noqa: E402


class TriageLoggerTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_triage_logger_for_tests()
        reset_microkernel_for_tests()
        reset_node_profile_for_tests()

    def test_resolve_db_path_shadow_profile(self) -> None:
        import os

        os.environ.pop("IG_TRIAGE_DB", None)
        os.environ["IG_NODE_PROFILE"] = "shadow"
        reset_node_profile_for_tests()
        path = resolve_triage_db_path()
        self.assertTrue(str(path).endswith("triage_v30.db"))

    def test_session_performance_tracker(self) -> None:
        tracker = SessionPerformanceTracker(baseline_gbp=10_000.0)
        tracker.record_closed_trade(50.0)
        tracker.record_closed_trade(-20.0)
        snap = tracker.current_snapshot()
        self.assertEqual(snap.trade_count, 2)
        self.assertEqual(snap.win_count, 1)
        self.assertEqual(snap.loss_count, 1)
        self.assertAlmostEqual(snap.net_pnl_sum, 30.0)
        self.assertGreaterEqual(snap.expectancy_gbp, 0.0)

    def test_slippage_quantify_overlap_premium(self) -> None:
        base = quantify_slippage(
            direction="BUY",
            requested_price=1.1000,
            fill_price=1.1003,
            spread_points=0.00015,
            session_window="neutral",
        )
        overlap = quantify_slippage(
            direction="BUY",
            requested_price=1.1000,
            fill_price=1.1003,
            spread_points=0.00015,
            session_window="london_us_overlap",
        )
        self.assertGreater(
            overlap["spread_penalty_points"],
            base["spread_penalty_points"],
        )

    def test_broker_fill_analysis(self) -> None:
        analysis = analyze_broker_fill_slippage(
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            requested_price=1.10000,
            broker_confirm={"level": 1.10005, "dealReference": "ABC123"},
            spread_points=0.00012,
        )
        self.assertAlmostEqual(analysis["fill_price"], 1.10005)
        self.assertGreater(analysis["slip_distance_points"], 0.0)

    def test_async_schema_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "triage_test.db"
            logger = TriageLogger(db_path=db_path)
            logger.start()
            logger.log_closed_position(
                ClosedPositionRecord(
                    ticket="T1",
                    asset="Gold",
                    size=1.0,
                    entry_price=4300.0,
                    exit_price=4310.0,
                    direction="BUY",
                    gross_pnl=10.0,
                    net_pnl=9.5,
                    exit_timestamp="2026-06-17 12:00:00",
                    epic="CS.D.CFPGOLD.CFP.IP",
                )
            )
            logger.log_latency_metric(
                LatencyMetricRecord(
                    timestamp=1710000000.0,
                    tick_arrival_us=1710000000.0 * 1_000_000.0,
                    processing_latency_us=42.0,
                    node_env="shadow",
                    epic="CS.D.CFPGOLD.CFP.IP",
                    slip_distance_points=0.5,
                    spread_penalty_points=0.3,
                )
            )
            logger.stop(timeout=3.0)

            async def _verify() -> tuple[int, int]:
                import aiosqlite

                async with aiosqlite.connect(str(db_path)) as db:
                    c1 = await db.execute("SELECT COUNT(*) FROM closed_positions")
                    row1 = await c1.fetchone()
                    c2 = await db.execute("SELECT COUNT(*) FROM latency_metrics")
                    row2 = await c2.fetchone()
                    return int(row1[0]), int(row2[0])

            closed_n, lat_n = asyncio.run(_verify())
            self.assertEqual(closed_n, 1)
            self.assertEqual(lat_n, 1)

    def test_legacy_settlement_mapping(self) -> None:
        rec = ClosedPositionRecord.from_legacy(
            ticket="X",
            asset="EUR/USD",
            size=2.0,
            entry=1.1,
            exit=1.1005,
            execution_side="SELL",
            gross_pnl=1.0,
            net_pnl=0.8,
            epic="CS.D.EURUSD.CFD.IP",
        )
        self.assertEqual(rec.direction, "SELL")
        self.assertEqual(rec.entry_price, 1.1)
        self.assertEqual(rec.exit_price, 1.1005)


if __name__ == "__main__":
    unittest.main()
