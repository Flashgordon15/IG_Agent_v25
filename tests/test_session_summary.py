"""Fix 6 — session summary file and macOS notification at session end."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.ml_training_store import MLTrainingStore, set_store_path_for_tests
from trading.points_engine import PointsEngine, set_points_state_path_for_tests
from trading.session_manager import SessionManager
from trading.session_summary import (
    SessionTickTracker,
    SessionTradeStats,
    build_summary_text,
    collect_session_trades,
    notify_macos,
    summary_filename_for,
    write_session_end_summary,
)
from trading.trading_loop import TradingLoop


class SessionSummaryFormatTests(unittest.TestCase):
    def test_build_summary_contains_required_fields(self) -> None:
        body = build_summary_text(
            close_time=datetime(2026, 5, 27, 21, 30),
            open_time=datetime(2026, 5, 27, 18, 0),
            trades=SessionTradeStats(total=2, wins=1, losses=1, pnl_gbp=12.5),
            points_delta=1.0,
            points_state="CAUTION",
            error_count=0,
            ml_records=2,
            stream_pct=95.5,
            top_block="rsi block",
        )
        for needle in (
            "IG Agent v29 — Session Summary",
            "Date: 2026-05-27",
            "Session:",
            "BST",
            "Trades:      2 (1W / 1L)",
            "Win rate:",
            "P&L:         £+12.50",
            "Points:",
            "Final state: CAUTION",
            "Errors:",
            "ML records:",
            "Stream uptime:",
            "Top block reason: rsi block",
        ):
            self.assertIn(needle, body)

    def test_zero_trades_session(self) -> None:
        body = build_summary_text(
            close_time=datetime(2026, 5, 27, 21, 0),
            open_time=datetime(2026, 5, 27, 18, 0),
            trades=SessionTradeStats(),
            points_delta=0.0,
            points_state="HEALTHY",
            error_count=0,
            ml_records=0,
            stream_pct=0.0,
            top_block="none",
        )
        self.assertIn("0 (0W / 0L)", body)
        self.assertIn("Win rate:    0.0%", body)

    def test_filename_uses_yyyymmdd(self) -> None:
        self.assertEqual(
            summary_filename_for(datetime(2026, 5, 27, 12, 0)),
            "session_summary_20260527.txt",
        )


class SessionSummaryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name) / "logs"
        self.logs.mkdir()
        self.db = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db))
        self.store.connect()
        self.ml_path = Path(self.tmp.name) / "ml.jsonl"
        set_store_path_for_tests(self.ml_path)
        set_points_state_path_for_tests(Path(self.tmp.name) / "points.json")
        self.points = PointsEngine(self.store)
        self.session = SessionManager(
            "IX.D.NIKKEI.IFM.IP",
            state_path=Path(self.tmp.name) / "session_state.json",
        )
        self.session._open_time = datetime(2026, 5, 27, 18, 0)  # noqa: SLF001
        self.tracker = SessionTickTracker()

    def tearDown(self) -> None:
        self.store.close()
        set_store_path_for_tests(None)
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    @patch("trading.session_summary.logs_dir")
    @patch("trading.session_summary.notify_macos")
    @patch("trading.session_summary.log_engine")
    def test_write_creates_file_with_correct_name(
        self, log_mock: MagicMock, notify_mock: MagicMock, logs_mock: MagicMock
    ) -> None:
        logs_mock.return_value = self.logs
        close_at = datetime(2026, 5, 27, 21, 0)
        path = write_session_end_summary(
            session=self.session,
            store=self.store,
            points=self.points,
            tracker=self.tracker,
            close_at=close_at,
            ml_store=MLTrainingStore(),
        )
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.name, "session_summary_20260527.txt")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Trades:      0 (0W / 0L)", text)
        notify_mock.assert_called_once()
        msg = notify_mock.call_args[0][0]
        self.assertIn("0W/0L", msg)
        self.assertIn("£+0.00", msg)
        log_mock.assert_any_call("Session summary written: 0W/0L £+0.00")

    @patch(
        "trading.session_summary.subprocess.run", side_effect=OSError("no osascript")
    )
    def test_osascript_failure_does_not_raise(self, _run: MagicMock) -> None:
        notify_macos("0W/0L £+0.00 HEALTHY")

    @patch("trading.session_summary.subprocess.run")
    def test_osascript_message_format(self, run_mock: MagicMock) -> None:
        notify_macos("2W/1L £+15.50 CAUTION")
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("2W/1L £+15.50 CAUTION", args[2])


class SessionTradesFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db))
        self.store.connect()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_collect_session_trades_filters_by_closed_at(self) -> None:
        open_t = datetime(2026, 5, 27, 18, 0)
        close_t = datetime(2026, 5, 27, 21, 0)
        self.store.conn.execute(
            """
            INSERT INTO trades (
                opened_at, closed_at, market, epic, side, entry, exit,
                size, pnl_points, result, dry_run, ig_pnl_currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                "2026-05-27 18:30:00",
                "2026-05-27 19:00:00",
                "Japan 225",
                "IX.D.NIKKEI.IFM.IP",
                "BUY",
                100.0,
                101.0,
                1.0,
                1.0,
                "WIN",
                10.0,
            ),
        )
        self.store.conn.execute(
            """
            INSERT INTO trades (
                opened_at, closed_at, market, epic, side, entry, exit,
                size, pnl_points, result, dry_run, ig_pnl_currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                "2026-05-26 19:00:00",
                "2026-05-26 20:00:00",
                "Japan 225",
                "IX.D.NIKKEI.IFM.IP",
                "SELL",
                100.0,
                99.0,
                1.0,
                1.0,
                "WIN",
                5.0,
            ),
        )
        self.store.conn.commit()
        stats = collect_session_trades(self.store, open_time=open_t, close_time=close_t)
        self.assertEqual(stats.total, 1)
        self.assertEqual(stats.wins, 1)
        self.assertEqual(stats.pnl_gbp, 10.0)


class TradingLoopSessionSummaryHookTests(unittest.TestCase):
    @patch("trading.trading_loop.publish_tick")
    @patch("trading.trading_loop.time.sleep")
    @patch("trading.trading_loop.write_session_end_summary")
    def test_flatten_confirmed_writes_summary(
        self,
        write_mock: MagicMock,
        _sleep: MagicMock,
        _snap: MagicMock,
    ) -> None:
        from data.models import Quote

        flatten = MagicMock(return_value=1)
        sync = MagicMock()
        sync.count_for_epic.return_value = 0
        session = MagicMock()
        session.should_run_flatten_attempt.return_value = True
        session.mark_flatten_attempt.return_value = 5.0
        session.is_entry_blocked_near_session_end.return_value = (False, None)
        session.is_session_open.return_value = True
        session.is_cold_start.return_value = False
        session.check_gap_open.return_value = False
        session.bars_since_open.return_value = 10
        session.on_tick = MagicMock()
        session.snapshot.return_value = MagicMock(phase="FLATTEN")
        session.session_open_time = datetime(2026, 5, 27, 18, 0)

        config = MagicMock(refresh_seconds=5.0, min_atr_points=0.0, currency_code="GBP")
        loop = TradingLoop(
            config=config,
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            session_manager=session,
            environment_scorer=MagicMock(),
            points_engine=MagicMock(snapshot=MagicMock(return_value=MagicMock())),
            signal_engine=MagicMock(
                add_quote=MagicMock(),
                quote_df=MagicMock(return_value=None),
            ),
            execution_loop=MagicMock(
                process_tick=MagicMock(),
                execution_engine=MagicMock(update_positions=MagicMock()),
            ),
            quote_source=lambda: Quote(datetime(2026, 5, 27, 21, 0), 100.0, 100.5),
            learning_store=MagicMock(sum_daily_pnl=MagicMock(return_value=0.0)),
            tick_interval_sec=0.05,
            on_flatten=flatten,
            position_sync=sync,
        )
        loop.run_once()
        write_mock.assert_called_once()
        session.flatten_confirmed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
