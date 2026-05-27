"""Tests for trading.session_manager."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from trading.session_manager import SessionManager


def _quote(px: float = 100.0, when: datetime | None = None) -> Quote:
    return Quote(when or datetime(2026, 5, 27, 1, 0), px - 0.25, px + 0.25)


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "session_state.json"
        self.points = MagicMock()
        self.env = MagicMock()
        self.engine = MagicMock()
        self.engine.quote_df.return_value = MagicMock()
        self.engine.candles.return_value = [1, 2, 3, 4, 5, 6, 7, 8]
        self.mgr = SessionManager(
            "IX.D.NIKKEI.IFM.IP",
            market="Japan 225",
            points_engine=self.points,
            environment_scorer=self.env,
            signal_engine=self.engine,
            state_path=self.state_path,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_is_session_open_safe_default(self) -> None:
        with patch(
            "trading.session_manager.is_market_open",
            side_effect=RuntimeError("fail"),
        ):
            self.assertFalse(self.mgr.is_session_open())

    def test_cold_start_under_six_bars(self) -> None:
        self.mgr._bars_at_open = 2
        self.engine.candles.return_value = list(range(6))
        self.assertEqual(self.mgr.bars_since_open(), 3)
        self.assertTrue(self.mgr.is_cold_start())
        self.engine.candles.return_value = list(range(10))
        self.assertFalse(self.mgr.is_cold_start())

    def test_gap_detection_registers_scorer(self) -> None:
        self.mgr._last_close_price = 100.0
        self.assertTrue(self.mgr.check_gap_open(10.0, open_price=115.0))
        self.env.register_gap_open.assert_called_once_with("Japan 225")
        self.assertTrue(self.mgr.check_gap_open(10.0, open_price=120.0))
        self.env.register_gap_open.assert_called_once()

    def test_should_flatten(self) -> None:
        with patch(
            "trading.session_manager.is_session_end_flatten_window",
            return_value=True,
        ):
            self.assertTrue(self.mgr.should_flatten())
        with patch(
            "trading.session_manager.is_session_end_flatten_window",
            return_value=False,
        ):
            self.assertFalse(self.mgr.should_flatten())

    def test_flatten_schedule_at_five_minutes(self) -> None:
        with patch.object(self.mgr, "minutes_to_session_end", return_value=4.8):
            self.assertTrue(self.mgr.should_run_flatten_attempt())
            self.assertEqual(self.mgr.mark_flatten_attempt(), 5.0)
        with patch.object(self.mgr, "minutes_to_session_end", return_value=4.8):
            self.assertFalse(self.mgr.should_run_flatten_attempt())

    def test_maintenance_reopen_resets_cold_start_not_points(self) -> None:
        self.mgr._last_close_time = datetime(2026, 5, 27, 21, 0)
        self.mgr.on_session_open(_quote(100.0), at=datetime(2026, 5, 27, 21, 45))
        self.points.reset_session.assert_not_called()
        self.env.reset_session.assert_called_once_with(
            "Japan 225",
            opened_at=datetime(2026, 5, 27, 21, 45),
            reset_cold_start_baseline=False,
        )
        self.assertEqual(self.mgr._maintenance_count_today, 1)

    def test_cold_start_advances_with_elapsed_time(self) -> None:
        self.mgr._open_time = datetime.now() - timedelta(minutes=16)
        self.mgr._bars_at_open = 0
        self.engine.candles.return_value = [1, 2]
        self.assertGreaterEqual(self.mgr.bars_since_open(), 3)
        self.assertTrue(self.mgr.is_cold_start())
        self.mgr._open_time = datetime.now() - timedelta(minutes=31)
        self.assertGreaterEqual(self.mgr.bars_since_open(), 6)
        self.assertFalse(self.mgr.is_cold_start())

    def test_new_day_open_resets_points_and_env(self) -> None:
        self.mgr._last_close_time = datetime(2026, 5, 26, 6, 0)
        self.mgr.on_session_open(_quote(100.0), at=datetime(2026, 5, 27, 1, 0))
        self.points.reset_session.assert_called_once()
        self.env.reset_session.assert_called_once()

    def test_transition_detection_on_tick(self) -> None:
        self.mgr._session_open = False
        seq = [True, True, False]

        def _open(*_a, **_k):
            return seq.pop(0) if seq else False

        with patch("trading.session_manager.is_market_open", side_effect=_open):
            with patch(
                "trading.session_manager.is_session_end_flatten_window",
                return_value=False,
            ):
                with patch.object(self.mgr, "_entry_atr_from_quote", return_value=0.0):
                    with patch.object(self.mgr, "on_session_open") as open_mock:
                        with patch.object(self.mgr, "on_session_close") as close_mock:
                            p1 = self.mgr.on_tick(_quote())
                            self.assertEqual(p1, "OPEN")
                            open_mock.assert_called_once()
                            p2 = self.mgr.on_tick(_quote())
                            self.assertEqual(p2, "OPEN")
                            p3 = self.mgr.on_tick(_quote())
                            self.assertEqual(p3, "CLOSED")
                            close_mock.assert_called_once()

    def test_flatten_phase_on_tick(self) -> None:
        self.mgr._session_open = True
        with patch("trading.session_manager.is_market_open", return_value=True):
            with patch(
                "trading.session_manager.is_session_end_flatten_window",
                return_value=True,
            ):
                self.assertEqual(self.mgr.on_tick(_quote()), "FLATTEN")

    def test_maintenance_phase_when_daily_break(self) -> None:
        self.mgr._session_open = False
        status = MagicMock()
        status.open = False
        status.reason = "daily break (maintenance)"
        with patch("trading.session_manager.is_market_open", return_value=False):
            with patch(
                "trading.session_manager.get_market_status",
                return_value=status,
            ):
                self.assertEqual(self.mgr.on_tick(_quote()), "MAINTENANCE")

    def test_daily_break_pause_preserves_session(self) -> None:
        self.mgr._session_open = True
        self.mgr._open_time = datetime(2026, 5, 27, 18, 0)
        self.mgr._bars_at_open = 2
        status = MagicMock()
        status.open = False
        status.reason = "daily break (maintenance)"
        with patch("trading.session_manager.is_market_open", return_value=False):
            with patch(
                "trading.session_manager.get_market_status",
                return_value=status,
            ):
                with patch.object(self.mgr, "on_session_close") as close_mock:
                    phase = self.mgr.on_tick(_quote())
                    self.assertEqual(phase, "MAINTENANCE")
                    close_mock.assert_not_called()
                    self.points.reset_session.assert_not_called()
                    self.assertTrue(self.mgr._session_open)

    def test_state_persistence_round_trip(self) -> None:
        self.mgr._session_open = True
        self.mgr._open_time = datetime(2026, 5, 27, 1, 0)
        self.mgr._gap_detected = True
        self.mgr._last_close_time = datetime(2026, 5, 26, 22, 0)
        self.mgr._last_close_price = 99.5
        self.mgr._maintenance_count_today = 2
        self.mgr._bars_at_open = 3
        self.engine.candles.return_value = list(range(10))
        self.mgr._persist(force=True)

        mgr2 = SessionManager(
            "IX.D.NIKKEI.IFM.IP",
            market="Japan 225",
            signal_engine=self.engine,
            state_path=self.state_path,
        )
        st = mgr2.get_state()
        self.assertTrue(st["session_open"])
        self.assertTrue(st["gap_detected"])
        self.assertEqual(st["maintenance_count_today"], 2)
        self.assertEqual(st["bars_elapsed"], 6)


if __name__ == "__main__":
    unittest.main()
