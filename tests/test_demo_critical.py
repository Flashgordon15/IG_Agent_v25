"""Pre-Demo critical path verification — session flatten calendar gap, ML/points wiring."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from execution.ml_training_hooks import configure_ml_training, get_points_engine
from trading.points_engine import PointsEngine, set_points_state_path_for_tests
from trading.session_manager import SessionManager


class SessionFlattenCalendarNoneTests(unittest.TestCase):
    """When fund calendar is missing, Japan 225 uses fallback session end."""

    def setUp(self) -> None:
        self.mgr = SessionManager("IX.D.NIKKEI.IFM.IP", market="Japan 225")

    @patch("system.market_watch.calendar.resolve_fund_for_epic", return_value=None)
    @patch("system.market_watch.calendar.is_market_open", return_value=True)
    def test_session_flatten_fires_when_calendar_returns_none(
        self, _mock_open: object, _mock_fund: object
    ) -> None:
        """Scheduled T-5 flatten must fire when fund calendar JSON is missing."""
        at = datetime(2026, 5, 28, 5, 55, tzinfo=ZoneInfo("Europe/London"))
        self.assertTrue(
            self.mgr.should_run_flatten_attempt(at=at),
            "expected flatten attempt with fallback calendar (T-5)",
        )
        self.assertTrue(
            self.mgr.should_flatten(at=at),
            "expected FLATTEN window with fallback calendar",
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
