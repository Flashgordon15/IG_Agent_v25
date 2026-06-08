"""
Tests for Week-1 24/7 hardening changes:
  - Friday auto-close + Sunday open blackout (order_validator)
  - max_position_age_minutes (trade_manager)
  - Nikkei signal_threshold 70 (config)
  - US Oil disabled (config)
  - Log rotation on startup (main)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Weekend gate check
# ---------------------------------------------------------------------------


def _validator_with_hours_enabled():
    from execution.order_validator import OrderValidator

    cfg = MagicMock()
    cfg.trading_hours_enabled = True
    cfg.epic = ""
    cfg.trading_session_whitelist = ["london_morning"]
    cfg.market_watch_enabled = False
    v = OrderValidator.__new__(OrderValidator)
    v._cfg = cfg
    return v


class TestWeekendGate:
    def _check(self, dt: datetime):
        from execution.order_validator import OrderValidator

        with patch("execution.order_validator.datetime") as mock_dt:
            mock_dt.utcnow.return_value = dt
            mock_dt.fromisoformat = datetime.fromisoformat
            return OrderValidator._weekend_gate_check()

    def test_friday_before_cutoff_allowed(self):
        # Friday 20:29 UTC — still allowed
        dt = datetime(2026, 6, 5, 20, 29)  # Friday
        ok, reason = self._check(dt)
        assert ok, reason

    def test_friday_at_cutoff_blocked(self):
        # Friday 20:30 UTC — blocked
        dt = datetime(2026, 6, 5, 20, 30)
        ok, reason = self._check(dt)
        assert not ok
        assert "Friday" in reason

    def test_friday_after_cutoff_blocked(self):
        dt = datetime(2026, 6, 5, 22, 0)
        ok, _ = self._check(dt)
        assert not ok

    def test_saturday_blocked(self):
        dt = datetime(2026, 6, 6, 12, 0)  # Saturday
        ok, reason = self._check(dt)
        assert not ok
        assert "Saturday" in reason

    def test_sunday_before_open_blocked(self):
        dt = datetime(2026, 6, 7, 21, 0)  # Sunday 21:00 UTC
        ok, _ = self._check(dt)
        assert not ok

    def test_sunday_open_blackout_blocked(self):
        dt = datetime(2026, 6, 7, 22, 5)  # Sunday 22:05 — within 15-min blackout
        ok, reason = self._check(dt)
        assert not ok
        assert "blackout" in reason.lower()

    def test_sunday_after_blackout_allowed(self):
        dt = datetime(2026, 6, 7, 22, 16)  # Sunday 22:16 — after blackout
        ok, _ = self._check(dt)
        assert ok

    def test_normal_weekday_not_affected(self):
        dt = datetime(2026, 6, 4, 14, 0)  # Thursday 14:00 UTC
        ok, _ = self._check(dt)
        assert ok


# ---------------------------------------------------------------------------
# Friday auto-close in trade_manager
# ---------------------------------------------------------------------------


class TestFridayAutoClose:
    def _make_manager(self):
        from trading.trade_manager import TradeManager

        tm = TradeManager.__new__(TradeManager)
        tm._rest = None
        tm._points_engine = None
        tm.on_alert = None
        tm._broker_stops = False
        return tm

    def test_not_friday_no_close(self):
        tm = self._make_manager()
        # Monday
        with patch("trading.trade_manager.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 6, 1, 20, 30)
            mock_dt.fromisoformat = datetime.fromisoformat
            result = tm._check_friday_close(
                "Gold", "SELL", 1, 4480.0, 4475.0, "", "epic", {}
            )
        assert result == []

    def test_friday_before_cutoff_no_close(self):
        tm = self._make_manager()
        with patch("trading.trade_manager.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 6, 5, 20, 29)
            mock_dt.fromisoformat = datetime.fromisoformat
            result = tm._check_friday_close(
                "Gold", "SELL", 1, 4480.0, 4475.0, "", "epic", {}
            )
        assert result == []

    def test_friday_at_cutoff_closes(self):
        tm = self._make_manager()
        store = MagicMock()
        tm.store = store
        tm._telegram_trade_closed = MagicMock()
        tm._telegram_alert = MagicMock()

        tr = {"size": 10.0}
        with patch("trading.trade_manager.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 6, 5, 20, 30)
            mock_dt.fromisoformat = datetime.fromisoformat
            msgs = tm._check_friday_close(
                "Gold", "SELL", 1, 4480.0, 4475.0, "", "epic", tr
            )

        assert len(msgs) == 1
        assert "FRIDAY AUTO-CLOSE" in msgs[0]
        store.close_trade.assert_called_once()


# ---------------------------------------------------------------------------
# max_position_age_minutes
# ---------------------------------------------------------------------------


class TestMaxPositionAge:
    def _make_manager(self, max_age: int = 480):
        from trading.trade_manager import TradeManager

        tm = TradeManager.__new__(TradeManager)
        cfg = MagicMock()
        cfg.max_position_age_minutes = max_age
        tm._cfg = cfg
        tm._rest = None
        tm._points_engine = None
        tm.on_alert = None
        return tm

    def _make_tr(self, opened_at: datetime):
        tr = MagicMock()
        tr.__getitem__ = lambda self, key: {
            "opened_at": opened_at.isoformat(),
            "size": 10.0,
        }[key]
        tr.keys = lambda: ["opened_at", "size"]
        return tr

    def test_young_position_not_closed(self):
        tm = self._make_manager(480)
        opened = datetime.utcnow() - timedelta(hours=4)
        tr = self._make_tr(opened)
        result = tm._check_max_position_age(
            "Gold", "SELL", 99, 4480.0, 4475.0, "", "epic", tr
        )
        assert result == []

    def test_old_position_closed(self):
        tm = self._make_manager(480)
        tm.store = MagicMock()
        tm._telegram_trade_closed = MagicMock()
        tm._telegram_alert = MagicMock()
        opened = datetime.utcnow() - timedelta(hours=9)
        tr = self._make_tr(opened)
        msgs = tm._check_max_position_age(
            "Gold", "SELL", 99, 4480.0, 4475.0, "", "epic", tr
        )
        assert len(msgs) == 1
        assert "MAX AGE CLOSE" in msgs[0]

    def test_no_max_age_config_skips(self):
        from trading.trade_manager import TradeManager

        tm = TradeManager.__new__(TradeManager)
        cfg = MagicMock()
        cfg.max_position_age_minutes = None
        tm._cfg = cfg
        opened = datetime.utcnow() - timedelta(hours=999)
        tr = self._make_tr(opened)
        result = tm._check_max_position_age(
            "Gold", "SELL", 99, 4480.0, 4475.0, "", "epic", tr
        )
        assert result == []


# ---------------------------------------------------------------------------
# Config checks
# ---------------------------------------------------------------------------


class TestWeek1Config:
    def _load(self):
        import json

        p = Path(__file__).parent.parent / "config" / "config_v25.json"
        with open(p) as f:
            return json.load(f)

    def test_nikkei_threshold_85(self):
        cfg = self._load()
        assert cfg["instruments"]["japan_225"]["signal_threshold"] == 85

    def test_oil_disabled(self):
        cfg = self._load()
        assert cfg["instruments"]["us_oil"]["enabled"] is False

    def test_max_position_age_set(self):
        cfg = self._load()
        assert cfg.get("max_position_age_minutes") == 480

    def test_wall_street_threshold_70(self):
        cfg = self._load()
        assert cfg["instruments"]["wall_street"]["signal_threshold"] == 70

    def test_dynamic_stop_floor_enabled(self):
        cfg = self._load()
        assert cfg.get("dynamic_stop_floor_enabled") is True

    def test_adaptive_min_setup_trades_6(self):
        cfg = self._load()
        assert cfg.get("adaptive_min_setup_trades") == 6


# ---------------------------------------------------------------------------
# Log rotation helper
# ---------------------------------------------------------------------------


class TestLogRotation:
    def test_oversized_log_gets_rotated(self, tmp_path):
        big_log = tmp_path / "launcher.log"
        big_log.write_bytes(b"x" * (25 * 1024 * 1024))  # 25 MB

        import main as m

        original_logs_dir = None
        try:
            import system.paths as paths_mod

            original_logs_dir = paths_mod.logs_dir

            def fake_logs_dir():
                return tmp_path

            paths_mod.logs_dir = fake_logs_dir
            old_max = m._LOG_ROTATE_MAX_BYTES
            m._LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024
            m._rotate_oversized_logs()
        finally:
            if original_logs_dir:
                paths_mod.logs_dir = original_logs_dir

        rotated = tmp_path / "launcher.log.1"
        assert rotated.exists(), "Oversized log should have been rotated to .1"
        assert big_log.exists(), "Fresh empty log file should exist after rotation"
        assert big_log.stat().st_size == 0

    def test_small_log_not_rotated(self, tmp_path):
        small_log = tmp_path / "engine.log"
        small_log.write_bytes(b"x" * 1024)

        import main as m
        import system.paths as paths_mod

        original = paths_mod.logs_dir

        def fake_logs_dir():
            return tmp_path

        try:
            paths_mod.logs_dir = fake_logs_dir
            m._rotate_oversized_logs()
        finally:
            paths_mod.logs_dir = original

        assert not (tmp_path / "engine.log.1").exists()
        assert small_log.stat().st_size == 1024
