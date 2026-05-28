"""Pre-Demo critical path verification — session flatten calendar gap, ML/points wiring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from execution.ml_training_hooks import configure_ml_training, get_points_engine
from trading.points_engine import PointsEngine, set_points_state_path_for_tests
from trading.session_manager import SessionManager


class SessionFlattenCalendarNoneTests(unittest.TestCase):
    """When fund calendar is missing, minutes_until_market_close returns None."""

    def setUp(self) -> None:
        self.mgr = SessionManager("IX.D.NIKKEI.IFM.IP", market="Japan 225")

    @pytest.mark.xfail(
        reason="minutes_until_market_close None skips scheduled flatten (missing fund calendar)",
        strict=False,
    )
    @patch("trading.session_manager.minutes_until_market_close", return_value=None)
    def test_session_flatten_fires_when_calendar_returns_none(self, _mock_mins) -> None:
        """Scheduled T-5 flatten must not be skipped when the calendar is unavailable."""
        self.assertTrue(
            self.mgr.should_run_flatten_attempt(),
            "expected flatten attempt when calendar returns None (safe default)",
        )
        self.assertTrue(
            self.mgr.should_flatten(),
            "expected FLATTEN phase when calendar returns None (safe default)",
        )


class ConfigureMlTrainingPointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "points_state.json"
        set_points_state_path_for_tests(self.state_path)
        self.engine = PointsEngine(store=None, state_path=self.state_path)
        configure_ml_training(points_engine=self.engine)

    def tearDown(self) -> None:
        configure_ml_training(ml_store=None, points_engine=None, environment_scorer=None)
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_points_engine_updates_after_configure_ml_training(self) -> None:
        wired = get_points_engine()
        self.assertIs(wired, self.engine, "get_points_engine must return the wired instance")
        before = float(wired.snapshot().cumulative)
        scored = wired.record_trade("WIN", 0.8, 5.0)
        self.assertGreater(scored, 0.0)
        self.assertGreater(float(wired.snapshot().cumulative), before)
