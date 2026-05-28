"""
Session-end flatten stress — mock open position at T-5 → FLATTEN CONFIRMED.

Run manually during maintenance window with live agent optional; this module
is the automated controlled test (no IG calls).
"""

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
from trading.trading_loop import TradingLoop


def _quote() -> Quote:
    return Quote(datetime(2026, 5, 27, 1, 0), 100.0, 100.5)


def _make_loop(**overrides) -> TradingLoop:
    config = MagicMock()
    config.refresh_seconds = 0.05
    config.max_spread_points = 35.0
    config.stop_distance_points = 40.0
    config.trade_size = 1.0
    config.currency_code = "GBP"
    config.get = MagicMock(return_value=1.0)
    config.min_atr_points = 0.0

    session = MagicMock()
    session.is_session_open.return_value = True
    session.is_cold_start.return_value = False
    session.check_gap_open.return_value = False
    session.bars_since_open.return_value = 10
    session.should_flatten.return_value = False
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
    points.trade_confidence_threshold.return_value = 80.0
    points.get_size_multiplier.return_value = 1.0
    points.snapshot.return_value = MagicMock(
        cumulative=0.0, session_score=0.0, last_trade_score=0.0
    )

    signal_engine = MagicMock()
    signal_engine.evaluate.return_value = MagicMock(
        signal="WAIT", adjusted_confidence=0.0, snapshot={}
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


class SessionFlattenStressTests(unittest.TestCase):
    @patch("trading.trading_loop.publish_tick")
    @patch("trading.trading_loop.time.sleep")
    def test_mock_open_position_flatten_confirmed_at_t5(
        self, sleep_mock: MagicMock, _snap: MagicMock
    ) -> None:
        """Inject open position → T-5 flatten attempt → verify closes and confirms."""
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
                any("FLATTEN CONFIRMED — all positions closed" in m for m in messages),
                f"expected FLATTEN CONFIRMED, got: {messages}",
            )
            session.flatten_confirmed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
