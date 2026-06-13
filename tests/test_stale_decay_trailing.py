"""Stale trailing distance compression — pure math + TradeManager wiring."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import TradeRecord
from execution.trailing_stop_engine import StaleDecayConfig, TrailEval, eval_trailing_stop
from system.config import Config
from trading.trade_manager import TradeManager


def _cfg(**overrides) -> Config:
    data = {
        "operating_mode": "DEMO",
        "account_type": "DEMO",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "auto_trade_enabled": True,
        "dry_run": True,
        "signal_threshold": 85,
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
        "breakeven_enabled": True,
        "breakeven_trigger_points": 30,
        "breakeven_lock_points": 0,
        "breakeven_offset_points": 0,
        "adaptive_trailing_stop_enabled": True,
        "adaptive_trailing_trigger_points": 30,
        "adaptive_trailing_distance_points": 25,
        "trailing_stop": {
            "stale_decay_activation_minutes": 15,
            "stale_decay_factor_per_minute": 0.02,
            "limit_extension_enabled": False,
        },
    }
    data.update(overrides)
    return Config(data)


def _open_trade(
    store: LearningStore,
    *,
    entry: float = 100.0,
    stop: float = 90.0,
    target: float = 200.0,
    opened_at: datetime | None = None,
) -> int:
    tid = store.open_trade(
        TradeRecord(
            id=None,
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            side="BUY",
            entry=entry,
            exit=None,
            size=1.0,
            stop=stop,
            target=target,
            pnl_points=None,
            result=None,
            confidence=90,
            adjusted_confidence=90,
            setup_key="BUY|bull|asia_early",
            dry_run=True,
            deal_reference="REF1",
            notes="",
        )
    )
    if opened_at is not None:
        store.conn.execute(
            "UPDATE trades SET opened_at=? WHERE id=?",
            (opened_at.isoformat(), tid),
        )
        store.conn.commit()
    return tid


class StaleDecayTrailingMathTests(unittest.TestCase):
    def test_buy_age_40_compresses_trail_distance_by_half(self) -> None:
        ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
        stale = StaleDecayConfig(trade_age_minutes=40.0)
        baseline = eval_trailing_stop(ev)
        decayed = eval_trailing_stop(ev, stale_decay=stale)
        self.assertAlmostEqual(baseline, 105.0)
        self.assertAlmostEqual(decayed, 107.5)

    def test_sell_compresses_stop_toward_market(self) -> None:
        ev = TrailEval("SELL", 100.0, 110.0, 80.0, 90.0, 10.0, 5.0, 5.0)
        stale = StaleDecayConfig(trade_age_minutes=40.0)
        baseline = eval_trailing_stop(ev)
        decayed = eval_trailing_stop(ev, stale_decay=stale)
        self.assertAlmostEqual(baseline, 95.0)
        self.assertAlmostEqual(decayed, 92.5)

    def test_at_mfe_bypass_returns_baseline_stop(self) -> None:
        ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
        stale = StaleDecayConfig(trade_age_minutes=40.0, at_mfe=True)
        self.assertAlmostEqual(
            eval_trailing_stop(ev, stale_decay=stale),
            eval_trailing_stop(ev),
        )

    def test_limit_extension_winning_bypass_returns_baseline_stop(self) -> None:
        ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
        stale = StaleDecayConfig(trade_age_minutes=40.0, limit_extension_winning=True)
        self.assertAlmostEqual(
            eval_trailing_stop(ev, stale_decay=stale),
            eval_trailing_stop(ev),
        )

    def test_missing_stale_decay_kwarg_is_backward_compatible(self) -> None:
        ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
        self.assertAlmostEqual(eval_trailing_stop(ev), 105.0)
        self.assertAlmostEqual(eval_trailing_stop(ev, stale_decay=None), 105.0)


class StaleDecayTradeManagerWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(str(Path(self.tmp.name) / "t.db"))
        self.store.connect()
        self.mgr = TradeManager(_cfg(), self.store, skip_ig_synced_exits=True)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    @patch("trading.trade_manager.log_engine")
    def test_trade_manager_applies_decay_when_trade_is_stale(self, _log) -> None:
        opened = datetime.utcnow() - timedelta(minutes=40)
        tid = _open_trade(self.store, stop=90.0, opened_at=opened)
        self.mgr._peak_profit_pts[tid] = 60.0
        msgs = self.mgr._apply_trailing(
            "Japan 225",
            "BUY",
            tid,
            100.0,
            90.0,
            200.0,
            150.0,
            trigger=10,
            distance=25,
            epic="IX.D.NIKKEI.IFM.IP",
            entry_atr=0.0,
        )
        self.assertTrue(msgs)
        new_stop = float(
            self.store.conn.execute(
                "SELECT stop FROM trades WHERE id=?", (tid,)
            ).fetchone()["stop"]
        )
        self.assertAlmostEqual(new_stop, 137.5, places=1)

    @patch("trading.trade_manager.log_engine")
    def test_trade_manager_defaults_without_explicit_stale_config(self, _log) -> None:
        cfg = _cfg(trailing_stop={})
        mgr = TradeManager(cfg, self.store, skip_ig_synced_exits=True)
        opened = datetime.utcnow() - timedelta(minutes=5)
        tid = _open_trade(self.store, stop=90.0, opened_at=opened)
        mgr._apply_trailing(
            "Japan 225",
            "BUY",
            tid,
            100.0,
            90.0,
            200.0,
            150.0,
            trigger=10,
            distance=25,
            epic="IX.D.NIKKEI.IFM.IP",
        )
        new_stop = float(
            self.store.conn.execute(
                "SELECT stop FROM trades WHERE id=?", (tid,)
            ).fetchone()["stop"]
        )
        self.assertAlmostEqual(new_stop, 125.0)


if __name__ == "__main__":
    unittest.main()
