"""Tests for entry protection fixes A–E."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.config import Config
from trading.entry_protection import (
    apply_ranging_penalty,
    check_daily_trade_cap,
    check_reentry_cooldown,
    check_session_blackout,
    increment_session_trade_count,
    ml_insufficient_data_threshold,
    record_epic_close,
    reset_entry_protection_state,
    session_trade_count,
    session_window_key,
)

GOLD = "CS.D.CFPGOLD.CFP.IP"
_LONDON = "Europe/London"


def _cfg(**overrides) -> Config:
    data = {
        "operating_mode": "DEMO",
        "account_type": "DEMO",
        "epic": GOLD,
        "auto_trade_enabled": True,
        "dry_run": True,
        "signal_threshold": 55,
        "trade_size": 1.0,
        "risk_points": 40,
        "reward_multiple": 2.0,
        "limit_distance_points": 80,
        "stop_distance_points": 40,
        "max_spread": 35,
        "max_spread_points": 35,
        "fast_ema": 9,
        "slow_ema": 21,
        "rsi_period": 14,
        "rsi_buy_min": 58,
        "rsi_buy_max": 68,
        "rsi_sell_max": 45,
        "atr_period": 14,
        "min_atr_points": 0,
        "momentum_gap_points": 5,
        "max_live_quotes": 5000,
        "learning_enabled": False,
        "vol_regime_filter_enabled": False,
        "entry_protection": {
            "enabled": True,
            "session_blackout_enabled": True,
            "gold_epic": GOLD,
            "gold_weekday_blackout_start": "20:00",
            "gold_weekday_blackout_end": "06:00",
            "gold_weekend_blackout_start": "Fri 20:00",
            "gold_weekend_blackout_end": "Mon 06:00",
            "cooldown_minutes_after_close": 10,
            "ranging_filter_enabled": True,
            "ranging_atr_ratio_threshold": 1.5,
            "ranging_h1_bars": 20,
            "h1_ranging_penalty": 25,
            "h1_ranging_hard_block": False,
            "max_trades_per_epic_per_session": 12,
            "ml_min_rows_for_trust": 50,
            "ml_insufficient_rows_threshold": 99,
        },
    }
    data.update(overrides)
    return Config(_data=data)


def _london_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(_LONDON))


def _seed_signal_engine(
    engine: SignalEngine,
    market: str,
    *,
    bars: int = 80,
    step: float = 1.0,
) -> None:
    base = datetime(2026, 6, 10, 8, 0)
    quotes = []
    price = 2000.0
    for i in range(bars):
        t = base + timedelta(minutes=i)
        quotes.append(Quote(t, price, price + 0.5))
        price += step
    engine.seed_ohlc_history(market, quotes)


class TestSessionBlackout(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    def test_weekday_1959_passes(self) -> None:
        cfg = _cfg()
        at = _london_dt(2026, 6, 15, 19, 59)
        blocked, reason = check_session_blackout(GOLD, cfg, now=at)
        self.assertFalse(blocked, reason)

    def test_weekday_2001_night_matrix_allowed(self) -> None:
        """Gold is night-matrix — legacy 20:00 weekday curfew deleted (v29.1)."""
        cfg = _cfg()
        at = _london_dt(2026, 6, 15, 20, 1)
        blocked, reason = check_session_blackout(GOLD, cfg, now=at)
        self.assertFalse(blocked, reason)

    def test_rollover_2200_blocks_gold(self) -> None:
        cfg = _cfg(
            entry_protection={
                "premium_overnight": {
                    "enabled": True,
                    "lockdown_permanent": True,
                    "epics": [GOLD],
                    "rollover_lock_start": "21:58",
                    "rollover_lock_end": "22:05",
                }
            }
        )
        at = _london_dt(2026, 6, 15, 22, 0)
        blocked, reason = check_session_blackout(GOLD, cfg, now=at)
        self.assertTrue(blocked)
        self.assertIn("rollover lock", reason.lower())

    def test_monday_1218_allowed(self) -> None:
        cfg = _cfg()
        at = _london_dt(2026, 6, 15, 12, 18)
        blocked, reason = check_session_blackout(GOLD, cfg, now=at, market="Gold")
        self.assertFalse(blocked, reason)

    def test_friday_2005_night_matrix_allowed(self) -> None:
        cfg = _cfg()
        at = _london_dt(2026, 6, 19, 20, 5)
        blocked, _ = check_session_blackout(GOLD, cfg, now=at, market="Gold")
        self.assertFalse(blocked)

    def test_monday_0559_night_matrix_allowed(self) -> None:
        cfg = _cfg()
        at = _london_dt(2026, 6, 15, 5, 59)
        blocked, _ = check_session_blackout(GOLD, cfg, now=at, market="Gold")
        self.assertFalse(blocked)

    def test_monday_0601_allowed(self) -> None:
        cfg = _cfg()
        at = _london_dt(2026, 6, 15, 6, 1)
        blocked, reason = check_session_blackout(GOLD, cfg, now=at, market="Gold")
        self.assertFalse(blocked, reason)


class TestReentryCooldown(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    def test_cooldown_blocks_then_releases(self) -> None:
        cfg = _cfg()
        epic = GOLD
        closed_at = _london_dt(2026, 6, 15, 10, 0)
        with patch("trading.entry_protection.datetime") as mock_dt:
            mock_dt.now.return_value = closed_at
            record_epic_close(epic, -5.0)

        inside = closed_at + timedelta(minutes=2)
        blocked, reason = check_reentry_cooldown(epic, cfg, now=inside)
        self.assertTrue(blocked)
        self.assertIn("remaining", reason)

        outside = closed_at + timedelta(minutes=11)
        blocked, reason = check_reentry_cooldown(epic, cfg, now=outside)
        self.assertFalse(blocked, reason)


class TestRangingPenalty(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    def _engine_with_h1(self, *, spread: float, atr: float, bars: int = 20, trending: bool = False) -> SignalEngine:
        cfg = _cfg()
        engine = SignalEngine(cfg)
        base = datetime(2026, 6, 10, 0, 0)
        rows = []
        mid = 100.0
        for i in range(bars * 60):
            t = base + timedelta(minutes=i)
            if trending:
                mid = 100.0 + i * 0.3
            rows.append(
                {
                    "time": t,
                    "bid": mid - 0.1,
                    "offer": mid + 0.1,
                    "mid": mid,
                    "spread": 0.2,
                }
            )
        df = pd.DataFrame(rows)
        c60 = engine.candles(df, 60)
        c60 = c60.copy()
        if not trending:
            low = 100.0
            high = low + spread
            c60["low"] = low
            c60["high"] = high
            c60["close"] = (low + high) / 2
            c60["price"] = c60["close"]
            c60["open"] = c60["close"]
        c60["atr"] = atr

        mock_engine = MagicMock(spec=SignalEngine)
        mock_engine.quote_df.return_value = df
        mock_engine.candles.side_effect = lambda d, m: c60 if m == 60 else engine.candles(d, m)
        mock_engine.add_indicators.side_effect = lambda d: d
        return mock_engine

    def test_ranging_applies_penalty(self) -> None:
        cfg = _cfg()
        engine = self._engine_with_h1(spread=5.0, atr=10.0)
        adjusted, penalty, note = apply_ranging_penalty(engine, "Gold", cfg, 70.0)
        self.assertEqual(penalty, 25.0)
        self.assertEqual(adjusted, 45.0)
        self.assertIn("ratio", note)

    def test_trending_no_penalty(self) -> None:
        cfg = _cfg()
        engine = self._engine_with_h1(spread=40.0, atr=10.0, trending=True)
        adjusted, penalty, note = apply_ranging_penalty(engine, "Gold", cfg, 70.0)
        self.assertEqual(penalty, 0.0)
        self.assertEqual(adjusted, 70.0)
        self.assertEqual(note, "")


class TestSessionTradeCap(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    def test_cap_blocks_at_limit(self) -> None:
        cfg = _cfg()
        epic = GOLD
        at = _london_dt(2026, 6, 15, 12, 0)
        for _ in range(12):
            increment_session_trade_count(epic, now=at)
        self.assertEqual(session_trade_count(epic, now=at), 12)
        blocked, reason = check_daily_trade_cap(epic, cfg, now=at)
        self.assertTrue(blocked)
        self.assertIn("12/12", reason)

    def test_cap_resets_at_london_open(self) -> None:
        cfg = _cfg()
        epic = GOLD
        late = _london_dt(2026, 6, 15, 6, 30)
        for _ in range(12):
            increment_session_trade_count(epic, now=late)
        open_am = _london_dt(2026, 6, 15, 7, 5)
        self.assertNotEqual(session_window_key(late), session_window_key(open_am))
        self.assertEqual(session_trade_count(epic, now=open_am), 0)
        blocked, _ = check_daily_trade_cap(epic, cfg, now=open_am)
        self.assertFalse(blocked)


class TestMlInsufficientDataGuard(unittest.TestCase):
    def test_forces_99_below_min_rows(self) -> None:
        cfg = _cfg()
        engine = SignalEngine(cfg)
        with patch(
            "trading.entry_protection.ml_training_record_count", return_value=14
        ):
            floor = ml_insufficient_data_threshold(cfg)
            self.assertEqual(floor, 99.0)
            threshold = engine._effective_signal_threshold(cfg)
        self.assertEqual(threshold, 99.0)

    def test_no_floor_at_or_above_min_rows(self) -> None:
        cfg = _cfg()
        with patch(
            "trading.entry_protection.ml_training_record_count", return_value=50
        ):
            floor = ml_insufficient_data_threshold(cfg)
        self.assertIsNone(floor)


class TestSignalEngineEntryProtection(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    def test_session_block_returns_wait_before_ml(self) -> None:
        cfg = _cfg()
        engine = SignalEngine(cfg)
        market = GOLD
        _seed_signal_engine(engine, market)
        with patch(
            "trading.entry_protection.check_session_blackout",
            return_value=(True, "test blackout"),
        ):
            result = engine.evaluate(market)
        self.assertEqual(result.signal, "WAIT")
        self.assertIn("session_blackout", result.setup_key)


if __name__ == "__main__":
    unittest.main()
