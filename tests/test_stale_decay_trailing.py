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
from config_test_helpers import trade_manager_test_config as _cfg
from trading.trade_manager import TradeManager


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

    @patch("trading.trade_manager.log_engine")
    def test_at_mfe_false_after_pullback_without_manual_peak_seed(self, _log) -> None:
        opened = datetime.utcnow() - timedelta(minutes=40)
        tid = _open_trade(self.store, stop=90.0, opened_at=opened)
        self.assertEqual(self.mgr._touch_peak_profit(tid, 20.0), 20.0)
        self.assertTrue(self.mgr._at_mfe(tid, 20.0))
        self.assertFalse(self.mgr._at_mfe(tid, 19.0))
        self.assertTrue(self.mgr._at_mfe(tid, 19.6))
        self.mgr._touch_peak_profit(tid, 25.0)
        self.assertFalse(self.mgr._at_mfe(tid, 24.0))


if __name__ == "__main__":
    unittest.main()
