"""
Overnight regression — replays Sunday 14 / Monday 15 June 2026 failure conditions.
"""

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
    record_epic_close,
    reset_entry_protection_state,
    session_trade_count,
)
from trading.flatten_retry import (
    flatten_backoff_seconds,
    flatten_max_retries,
    flatten_slow_monitor_interval,
    get_flatten_retry_state,
    on_flatten_confirmed,
    on_flatten_verify_failed,
    reset_flatten_retry_state,
)

GOLD = "CS.D.CFPGOLD.CFP.IP"
_LONDON = "Europe/London"


def _cfg() -> Config:
    return Config(
        _data={
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
            },
            "flatten_retry": {
                "flatten_max_retries": 5,
                "flatten_retry_backoff_seconds": [30, 60, 120, 240, 480],
                "flatten_slow_monitor_interval_seconds": 600,
            },
        }
    )


def _london(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(_LONDON))


def _ranging_engine(spread: float, atr: float, *, trending: bool = False) -> MagicMock:
    cfg = _cfg()
    engine = SignalEngine(cfg)
    base = datetime(2026, 6, 10, 0, 0)
    rows = []
    for i in range(20 * 60):
        t = base + timedelta(minutes=i)
        mid = 100.0 + (i * 0.3 if trending else 0.0)
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
    c60 = engine.candles(df, 60).copy()
    if not trending:
        c60["low"] = 100.0
        c60["high"] = 100.0 + spread
        c60["close"] = 100.0 + spread / 2
        c60["price"] = c60["close"]
        c60["open"] = c60["close"]
    c60["atr"] = atr
    mock = MagicMock(spec=SignalEngine)
    mock.quote_df.return_value = df
    mock.candles.side_effect = lambda d, m: c60 if m == 60 else engine.candles(d, m)
    mock.add_indicators.side_effect = lambda d: d
    return mock


class Scenario1BstBlackout(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    @patch("trading.entry_protection.log_engine")
    def test_sunday_2205_blocked(self, mock_log) -> None:
        cfg = _cfg()
        at = _london(2026, 6, 14, 22, 5)
        blocked, _ = check_session_blackout(GOLD, cfg, now=at, market="Gold")
        self.assertTrue(blocked)
        logs = " ".join(str(c[0][0]) for c in mock_log.call_args_list)
        self.assertIn("[SESSION BLOCK]", logs)

    def test_signal_wait_not_execution(self) -> None:
        cfg = _cfg()
        engine = SignalEngine(cfg)
        base = datetime(2026, 6, 10, 8, 0)
        quotes = [Quote(base + timedelta(minutes=i), 2000 + i, 2000.5 + i) for i in range(80)]
        engine.seed_ohlc_history(GOLD, quotes)
        at = _london(2026, 6, 14, 22, 5)
        with patch(
            "trading.entry_protection.check_session_blackout",
            return_value=(True, "weekend blackout"),
        ):
            result = engine.evaluate(GOLD)
        self.assertEqual(result.signal, "WAIT")


class Scenario2Cooldown(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    @patch("trading.entry_protection.log_engine")
    def test_cooldown_block_and_release(self, mock_log) -> None:
        cfg = _cfg()
        t0 = _london(2026, 6, 15, 10, 0)
        with patch("trading.entry_protection.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            record_epic_close(GOLD, 50.0)
        blocked, _ = check_reentry_cooldown(GOLD, cfg, now=t0 + timedelta(minutes=2))
        self.assertTrue(blocked)
        logs = " ".join(str(c[0][0]) for c in mock_log.call_args_list)
        self.assertIn("[COOLDOWN]", logs)
        blocked, _ = check_reentry_cooldown(GOLD, cfg, now=t0 + timedelta(minutes=11))
        self.assertFalse(blocked)


class Scenario3SessionCap(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    @patch("trading.entry_protection.log_engine")
    def test_cap_and_reset(self, mock_log) -> None:
        cfg = _cfg()
        late = _london(2026, 6, 15, 6, 30)
        for _ in range(12):
            increment_session_trade_count(GOLD, now=late)
        blocked, reason = check_daily_trade_cap(GOLD, cfg, now=late)
        self.assertTrue(blocked)
        self.assertIn("12/12", reason)
        logs = " ".join(str(c[0][0]) for c in mock_log.call_args_list)
        self.assertIn("[SESSION CAP]", logs)
        after_open = _london(2026, 6, 15, 7, 5)
        self.assertEqual(session_trade_count(GOLD, now=after_open), 0)
        blocked, _ = check_daily_trade_cap(GOLD, cfg, now=after_open)
        self.assertFalse(blocked)


class Scenario4RangingPenalty(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()

    @patch("trading.entry_protection.log_engine")
    def test_penalty_and_trend_pass(self, mock_log) -> None:
        cfg = _cfg()
        choppy = _ranging_engine(spread=5.0, atr=10.0)
        adjusted, penalty, _ = apply_ranging_penalty(choppy, "Gold", cfg, 70.0)
        self.assertEqual(penalty, 25.0)
        self.assertEqual(adjusted, 45.0)
        logs = " ".join(str(c[0][0]) for c in mock_log.call_args_list)
        self.assertIn("[REGIME PENALTY]", logs)
        trending = _ranging_engine(spread=40.0, atr=10.0, trending=True)
        adjusted2, penalty2, _ = apply_ranging_penalty(trending, "Gold", cfg, 70.0)
        self.assertEqual(penalty2, 0.0)
        self.assertEqual(adjusted2, 70.0)


class Scenario5FlattenRetry(unittest.TestCase):
    def setUp(self) -> None:
        reset_flatten_retry_state()

    def test_retry_schedule(self) -> None:
        cfg = _cfg()
        self.assertEqual(flatten_max_retries(cfg), 5)
        self.assertEqual(flatten_backoff_seconds(cfg), [30, 60, 120, 240, 480])
        self.assertEqual(flatten_slow_monitor_interval(cfg), 600.0)
        notify = MagicMock()
        t0 = 0.0
        for i in range(5):
            st = on_flatten_verify_failed(GOLD, 1, cfg=cfg, now=t0 + i, notify=notify)
        self.assertTrue(st.abandoned)
        self.assertTrue(st.slow_monitor_active)
        notify.assert_called()
        on_flatten_confirmed()
        self.assertEqual(get_flatten_retry_state().retry_count, 0)


class Scenario6FullOvernight(unittest.TestCase):
    def setUp(self) -> None:
        reset_entry_protection_state()
        reset_flatten_retry_state()

    def test_combined_guards_limit_round_trips(self) -> None:
        cfg = _cfg()
        round_trips = 0
        blocked_night = 0
        night_checks = 0
        for hour in range(20, 24):
            at = _london(2026, 6, 14, hour, 5)
            night_checks += 1
            blocked, _ = check_session_blackout(GOLD, cfg, now=at)
            if blocked:
                blocked_night += 1
            else:
                round_trips += 1
        for hour in range(0, 6):
            at = _london(2026, 6, 15, hour, 5)
            night_checks += 1
            blocked, _ = check_session_blackout(GOLD, cfg, now=at)
            if blocked:
                blocked_night += 1
        self.assertEqual(blocked_night, night_checks)
        self.assertLess(round_trips, 20)
        self.assertLess(round_trips, 108)
        late = _london(2026, 6, 15, 6, 30)
        for _ in range(12):
            increment_session_trade_count(GOLD, now=late)
        blocked, _ = check_daily_trade_cap(GOLD, cfg, now=late)
        self.assertTrue(blocked)
        st = get_flatten_retry_state()
        for _ in range(5):
            on_flatten_verify_failed(GOLD, 1, cfg=cfg)
        st = get_flatten_retry_state()
        self.assertLessEqual(st.retry_count, 5)


if __name__ == "__main__":
    unittest.main()
