"""Fix 3 — session-end flatten verification, retries, and entry block."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from trading.session_manager import (
    ENTRY_BLOCK_MINUTES,
    FLATTEN_RETRY_MINUTES,
    SessionManager,
)
from trading.trading_loop import TradingLoop


def _quote() -> Quote:
    return Quote(datetime(2026, 5, 27, 1, 0), 100.0, 100.5)


class SessionManagerFlattenScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = SessionManager(
            "IX.D.NIKKEI.IFM.IP",
            market="Japan 225",
            state_path=Path(self.tmp.name) / "session_state.json",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_minutes_to_session_end_delegates(self) -> None:
        with patch(
            "trading.session_manager.minutes_until_market_close",
            return_value=4.5,
        ) as mock_mins:
            self.assertEqual(self.mgr.minutes_to_session_end(), 4.5)
            mock_mins.assert_called_once()

    def test_flatten_attempt_at_t5_t3_t1(self) -> None:
        with patch.object(self.mgr, "minutes_to_session_end", return_value=5.0):
            self.assertTrue(self.mgr.should_run_flatten_attempt())
            self.mgr.mark_flatten_attempt()
        with patch.object(self.mgr, "minutes_to_session_end", return_value=4.0):
            self.assertFalse(self.mgr.should_run_flatten_attempt())
        with patch.object(self.mgr, "minutes_to_session_end", return_value=3.0):
            self.assertTrue(self.mgr.should_run_flatten_attempt())
            self.mgr.mark_flatten_attempt()
        with patch.object(self.mgr, "minutes_to_session_end", return_value=1.0):
            self.assertTrue(self.mgr.should_run_flatten_attempt())

    def test_entry_blocked_under_ten_minutes(self) -> None:
        with patch.object(self.mgr, "minutes_to_session_end", return_value=9.0):
            blocked, mins = self.mgr.is_entry_blocked_near_session_end()
            self.assertTrue(blocked)
            self.assertEqual(mins, 9)
        with patch.object(self.mgr, "minutes_to_session_end", return_value=10.0):
            blocked, mins = self.mgr.is_entry_blocked_near_session_end()
            self.assertFalse(blocked)
            self.assertIsNone(mins)

    def test_flatten_confirmed_stops_attempts(self) -> None:
        self.mgr.flatten_confirmed()
        with patch.object(self.mgr, "minutes_to_session_end", return_value=1.0):
            self.assertFalse(self.mgr.should_run_flatten_attempt())

    def test_retry_threshold_constants(self) -> None:
        self.assertEqual(FLATTEN_RETRY_MINUTES, (5.0, 3.0, 1.0))
        self.assertEqual(ENTRY_BLOCK_MINUTES, 10.0)


def _make_loop(**overrides) -> TradingLoop:
    config = MagicMock()
    config.refresh_seconds = 0.05
    config.max_spread_points = 35.0
    config.stop_distance_points = 40.0
    config.trade_size = 1.0
    config.currency_code = "GBP"
    config.get = MagicMock(return_value=1.0)

    session = MagicMock()
    session.is_session_open.return_value = True
    session.is_cold_start.return_value = False
    session.check_gap_open.return_value = False
    session.bars_since_open.return_value = 10
    session.should_run_flatten_attempt.return_value = False
    session.is_entry_blocked_near_session_end.return_value = (False, None)
    session.on_tick = MagicMock()
    session.snapshot.return_value = MagicMock(phase="OPEN")

    env = MagicMock()
    env.score.return_value = 55.0

    points = MagicMock()
    points.get_state.return_value = "HEALTHY"
    points.is_session_paused.return_value = False
    points.is_day_stopped.return_value = False
    points.get_threshold.return_value = 80.0
    points.get_size_multiplier.return_value = 1.0
    points.snapshot.return_value = MagicMock(
        cumulative=0.0,
        session_score=0.0,
        last_trade_score=0.0,
    )

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = MagicMock(
        signal="WAIT",
        adjusted_confidence=0.0,
        snapshot={},
    )
    signal_engine.quote_df.return_value = None

    exec_engine = MagicMock()
    exec_engine.trade_tracker.count_open_for_epic.return_value = 0
    exec_engine.trade_tracker.snapshot.return_value = {"positions": []}
    exec_engine.update_positions = MagicMock()

    execution_loop = MagicMock()
    execution_loop.execution_engine = exec_engine
    execution_loop.process_tick = MagicMock()

    kwargs = {
        "config": config,
        "market": "Japan 225",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "session_manager": session,
        "environment_scorer": env,
        "points_engine": points,
        "signal_engine": signal_engine,
        "execution_loop": execution_loop,
        "quote_source": lambda: _quote(),
        "learning_store": MagicMock(sum_daily_pnl=MagicMock(return_value=0.0)),
        "tick_interval_sec": 0.05,
    }
    kwargs.update(overrides)
    return TradingLoop(**kwargs)


class TradingLoopFlattenVerificationTests(unittest.TestCase):
    @patch("trading.trading_loop.publish_tick")
    @patch("trading.trading_loop.time.sleep")
    def test_flatten_at_t5_with_confirmation(
        self, sleep_mock: MagicMock, _snap: MagicMock
    ) -> None:
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

        loop = _make_loop(
            on_flatten=flatten,
            position_sync=sync,
            session_manager=session,
        )
        with patch("trading.trading_loop.log_engine") as log_mock:
            loop.run_once()
            flatten.assert_called_once()
            sync.sync_once.assert_called_once()
            sleep_mock.assert_called_once()
            messages = [c.args[0] for c in log_mock.call_args_list]
            self.assertTrue(
                any("FLATTEN CONFIRMED — all positions closed" in m for m in messages)
            )
            session.flatten_confirmed.assert_called_once()

    @patch("trading.trading_loop.publish_tick")
    @patch("trading.trading_loop.time.sleep")
    def test_flatten_retries_when_still_open(
        self, sleep_mock: MagicMock, _snap: MagicMock
    ) -> None:
        flatten = MagicMock(return_value=1)
        sync = MagicMock()
        sync.count_for_epic.return_value = 1
        session = MagicMock()
        session.should_run_flatten_attempt.return_value = True
        session.mark_flatten_attempt.return_value = 5.0
        session.record_flatten_failure.return_value = 1
        session.flatten_failures.return_value = 1
        session.is_entry_blocked_near_session_end.return_value = (False, None)
        session.is_session_open.return_value = True
        session.is_cold_start.return_value = False
        session.check_gap_open.return_value = False
        session.bars_since_open.return_value = 10
        session.on_tick = MagicMock()
        session.snapshot.return_value = MagicMock(phase="FLATTEN")

        loop = _make_loop(
            on_flatten=flatten,
            position_sync=sync,
            session_manager=session,
        )
        with patch("trading.trading_loop.log_engine") as log_mock:
            loop.run_once()
            flatten.assert_called_once()
            session.record_flatten_failure.assert_called_once()
            messages = [c.args[0] for c in log_mock.call_args_list]
            self.assertTrue(
                any("flatten verify failed" in m and "still open" in m for m in messages)
            )

    @patch("trading.trading_loop.publish_tick")
    @patch("trading.trading_loop.time.sleep")
    @patch("trading.trading_loop.subprocess.Popen")
    def test_emergency_stop_after_three_failures(
        self,
        popen_mock: MagicMock,
        sleep_mock: MagicMock,
        _snap: MagicMock,
    ) -> None:
        flatten = MagicMock(return_value=1)
        sync = MagicMock()
        sync.count_for_epic.return_value = 2
        session = MagicMock()
        session.should_run_flatten_attempt.return_value = True
        session.mark_flatten_attempt.return_value = 1.0
        session.record_flatten_failure.return_value = 3
        session.flatten_failures.return_value = 3
        session.is_entry_blocked_near_session_end.return_value = (False, None)
        session.is_session_open.return_value = True
        session.is_cold_start.return_value = False
        session.check_gap_open.return_value = False
        session.bars_since_open.return_value = 10
        session.on_tick = MagicMock()
        session.snapshot.return_value = MagicMock(phase="FLATTEN")

        loop = _make_loop(
            on_flatten=flatten,
            position_sync=sync,
            session_manager=session,
        )
        with patch("trading.trading_loop.log_engine") as log_mock:
            loop.run_once()
            messages = [c.args[0] for c in log_mock.call_args_list]
            self.assertTrue(
                any(
                    "CRITICAL: FLATTEN FAILED — manual intervention required" in m
                    for m in messages
                )
            )
            popen_mock.assert_called_once()

    @patch("trading.trading_loop.publish_tick")
    def test_entry_blocked_at_t10min(self, _snap: MagicMock) -> None:
        session = MagicMock()
        session.is_entry_blocked_near_session_end.return_value = (True, 9)
        session.is_session_open.return_value = True
        session.is_cold_start.return_value = False
        session.check_gap_open.return_value = False
        session.bars_since_open.return_value = 10
        session.should_run_flatten_attempt.return_value = False
        session.on_tick = MagicMock()
        session.snapshot.return_value = MagicMock(phase="OPEN")

        loop = _make_loop(session_manager=session)
        ctx = loop.run_once()
        assert ctx is not None
        gate = next(g for g in ctx.gates if g.name == "session_open")
        self.assertFalse(gate.passed)
        self.assertIn("entry blocked — session ends in 9min", gate.detail)
        loop._execution_loop.process_tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
