"""Tests for trading.trade_manager v25 extensions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import Quote, TradeRecord
from system.config import Config
from trading.points_engine import PointsEngine
from trading.trade_manager import (
    HARD_CAP_ATR_MULTIPLE,
    PARTIAL_CLOSE_ATR_MULTIPLE,
    TradeManager,
)


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
        "learning_enabled": False,
        "max_live_quotes": 1000,
    }
    data.update(overrides)
    return Config(_data=data)


def _open_trade(store: LearningStore, *, entry: float = 100.0, stop: float = 90.0) -> int:
    return store.open_trade(
        TradeRecord(
            id=None,
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            side="BUY",
            entry=entry,
            exit=None,
            size=2.0,
            stop=stop,
            target=entry + 100,
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


def _open_sell_trade(store: LearningStore, *, entry: float = 100.0, stop: float = 110.0) -> int:
    return store.open_trade(
        TradeRecord(
            id=None,
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            side="SELL",
            entry=entry,
            exit=None,
            size=2.0,
            stop=stop,
            target=entry - 100,
            pnl_points=None,
            result=None,
            confidence=90,
            adjusted_confidence=90,
            setup_key="SELL|bear|asia_early",
            dry_run=True,
            deal_reference="REF2",
            notes="",
        )
    )


class TrailDistanceTests(unittest.TestCase):
    def test_get_trail_distance_bands(self) -> None:
        atr = 20.0
        self.assertAlmostEqual(TradeManager.get_trail_distance(93, atr), 35.0)
        self.assertAlmostEqual(TradeManager.get_trail_distance(88, atr), 30.0)
        self.assertAlmostEqual(TradeManager.get_trail_distance(82, atr), 20.0)

    def test_get_trail_distance_safe_default(self) -> None:
        dist = TradeManager.get_trail_distance("bad", 10.0)  # type: ignore[arg-type]
        self.assertAlmostEqual(dist, 15.0)

    def test_confidence_band_labels(self) -> None:
        self.assertEqual(TradeManager.confidence_band(95), "high")
        self.assertEqual(TradeManager.confidence_band(87), "standard")
        self.assertEqual(TradeManager.confidence_band(81), "marginal")


class TradeManagerExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(str(Path(self.tmp.name) / "t.db"))
        self.store.connect()
        self.points_path = Path(self.tmp.name) / "points.json"
        self.points = PointsEngine(self.store, state_path=self.points_path)
        self.mgr = TradeManager(
            _cfg(),
            self.store,
            skip_ig_synced_exits=True,
            points_engine=self.points,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_entry_stores_trail_meta(self) -> None:
        quote = Quote(datetime.now(), 100.0, 100.5)
        tid = self.mgr.open_trade_from_execution(
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            side="BUY",
            quote=quote,
            raw_confidence=93,
            adjusted_confidence=93,
            setup_key="k",
            deal_reference="R1",
            notes="",
            execution={"atr": 20.0, "size": 1.0, "risk": 40, "limit": 80},
            dry_run=True,
        )
        row = self.store.conn.execute(
            "SELECT confidence_band, entry_atr, trail_distance FROM trades WHERE id=?",
            (tid,),
        ).fetchone()
        self.assertEqual(row["confidence_band"], "high")
        self.assertAlmostEqual(float(row["entry_atr"]), 20.0)
        self.assertAlmostEqual(float(row["trail_distance"]), 35.0)

    def test_trailing_uses_atr_distance_not_fixed_step(self) -> None:
        entry = 100.0
        tid = _open_trade(self.store, entry=entry, stop=80.0)
        self.store.set_v25_entry_meta(
            tid, confidence_band="high", entry_atr=20.0, trail_distance=35.0
        )
        self.store.conn.execute("UPDATE trades SET target=? WHERE id=?", (entry + 200, tid))
        self.store.conn.commit()
        cfg = _cfg(
            breakeven_enabled=False,
            adaptive_trailing_trigger_points=10,
            adaptive_trailing_distance_points=5,
        )
        mgr = TradeManager(cfg, self.store, skip_ig_synced_exits=True)
        px = entry + 50
        msgs = mgr.update_from_quote(
            "Japan 225", "IX.D.NIKKEI.IFM.IP", Quote(datetime.now(), px, px + 1)
        )
        self.assertTrue(any("TRAILING" in m for m in msgs))
        stop = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(stop, px - 35.0, places=1)

    def test_breakeven_still_works(self) -> None:
        entry = 100.0
        tid = _open_trade(self.store, entry=entry, stop=80.0)
        cfg = _cfg(adaptive_trailing_stop_enabled=False)
        mgr = TradeManager(cfg, self.store, skip_ig_synced_exits=True)
        px = entry + 35
        msgs = mgr.update_from_quote(
            "Japan 225", "IX.D.NIKKEI.IFM.IP", Quote(datetime.now(), px, px + 1)
        )
        self.assertTrue(any("BREAKEVEN" in m for m in msgs))
        stop = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(stop, entry, places=1)

    def test_partial_close_once_and_points(self) -> None:
        entry = 100.0
        tid = _open_trade(self.store, entry=entry)
        self.store.set_v25_entry_meta(
            tid, confidence_band="high", entry_atr=20.0, trail_distance=35.0
        )
        mgr = TradeManager(
            _cfg(breakeven_enabled=False, adaptive_trailing_stop_enabled=False),
            self.store,
            skip_ig_synced_exits=True,
            points_engine=self.points,
        )
        px = entry + PARTIAL_CLOSE_ATR_MULTIPLE * 20.0 + 1.0
        quote = Quote(datetime.now(), px, px + 1)
        msgs1 = mgr.update_from_quote("Japan 225", "IX.D.NIKKEI.IFM.IP", quote)
        self.assertTrue(any("PARTIAL CLOSE" in m for m in msgs1))
        size_after = float(
            self.store.conn.execute("SELECT size FROM trades WHERE id=?", (tid,)).fetchone()["size"]
        )
        self.assertAlmostEqual(size_after, 1.0)
        self.assertTrue(self.store.is_partial_close_done(tid))
        self.assertGreater(self.points._cumulative, 0)

        msgs2 = mgr.update_from_quote("Japan 225", "IX.D.NIKKEI.IFM.IP", quote)
        self.assertFalse(any("PARTIAL CLOSE" in m for m in msgs2))

    def test_hard_cap_closes_position(self) -> None:
        entry = 100.0
        tid = _open_trade(self.store, entry=entry)
        self.store.set_v25_entry_meta(
            tid, confidence_band="standard", entry_atr=10.0, trail_distance=15.0
        )
        px = entry + HARD_CAP_ATR_MULTIPLE * 10.0 + 5.0
        msgs = self.mgr.update_from_quote(
            "Japan 225", "IX.D.NIKKEI.IFM.IP", Quote(datetime.now(), px, px + 1)
        )
        self.assertTrue(any("HARD CAP" in m for m in msgs))
        row = self.store.conn.execute(
            "SELECT closed_at FROM trades WHERE id=?", (tid,)
        ).fetchone()
        self.assertIsNotNone(row["closed_at"])

    def test_trail_only_moves_in_profit_direction(self) -> None:
        entry = 100.0
        tid = _open_trade(self.store, entry=entry, stop=95.0)
        self.store.set_v25_entry_meta(
            tid, confidence_band="high", entry_atr=20.0, trail_distance=35.0
        )
        cfg = _cfg(breakeven_enabled=False, adaptive_trailing_trigger_points=5)
        mgr = TradeManager(cfg, self.store, skip_ig_synced_exits=True)
        px = entry + 20
        mgr.update_from_quote(
            "Japan 225", "IX.D.NIKKEI.IFM.IP", Quote(datetime.now(), px, px + 1)
        )
        stop_high = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        px_low = entry + 5
        mgr.update_from_quote(
            "Japan 225", "IX.D.NIKKEI.IFM.IP", Quote(datetime.now(), px_low, px_low + 1)
        )
        stop_after = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(stop_after, stop_high, places=1)


class TrailDirectionAssertionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(str(Path(self.tmp.name) / "t.db"))
        self.store.connect()
        self.mgr = TradeManager(_cfg(), self.store, skip_ig_synced_exits=True)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    @patch("trading.trade_manager.log_engine")
    def test_buy_trail_accepted_when_stop_rises(self, mock_log: MagicMock) -> None:
        entry, stop, target = 100.0, 90.0, 200.0
        tid = _open_trade(self.store, entry=entry, stop=stop)
        px = 150.0
        msgs = self.mgr._apply_trailing(
            "Japan 225", "BUY", tid, entry, stop, target, px, trigger=10, distance=25
        )
        self.assertTrue(msgs)
        mock_log.assert_not_called()
        new_stop = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(new_stop, px - 25)

    @patch("trading.trade_manager.log_engine")
    def test_buy_trail_rejected_when_stop_would_fall(self, mock_log: MagicMock) -> None:
        entry, stop, target = 100.0, 115.0, 200.0
        tid = _open_trade(self.store, entry=entry, stop=stop)
        px = 120.0
        msgs = self.mgr._apply_trailing(
            "Japan 225", "BUY", tid, entry, stop, target, px, trigger=10, distance=25
        )
        self.assertEqual(msgs, [])
        mock_log.assert_called_once()
        msg = mock_log.call_args[0][0]
        self.assertIn("ERROR: Trail would move stop backwards", msg)
        self.assertIn("current=115", msg)
        self.assertIn("proposed=95", msg)
        unchanged = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(unchanged, stop)

    @patch("trading.trade_manager.log_engine")
    def test_sell_trail_accepted_when_stop_lowers(self, mock_log: MagicMock) -> None:
        entry, stop, target = 100.0, 110.0, 0.0
        tid = _open_sell_trade(self.store, entry=entry, stop=stop)
        px = 80.0
        msgs = self.mgr._apply_trailing(
            "Japan 225", "SELL", tid, entry, stop, target, px, trigger=10, distance=25
        )
        self.assertTrue(msgs)
        mock_log.assert_not_called()
        new_stop = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(new_stop, px + 25)

    @patch("trading.trade_manager.log_engine")
    def test_sell_trail_rejected_when_stop_would_rise(self, mock_log: MagicMock) -> None:
        entry, stop, target = 100.0, 85.0, 0.0
        tid = _open_sell_trade(self.store, entry=entry, stop=stop)
        px = 90.0
        msgs = self.mgr._apply_trailing(
            "Japan 225", "SELL", tid, entry, stop, target, px, trigger=10, distance=25
        )
        self.assertEqual(msgs, [])
        mock_log.assert_called_once()
        msg = mock_log.call_args[0][0]
        self.assertEqual(msg, "ERROR: Trail would move stop backwards — rejected.")
        unchanged = float(
            self.store.conn.execute("SELECT stop FROM trades WHERE id=?", (tid,)).fetchone()["stop"]
        )
        self.assertAlmostEqual(unchanged, stop)


if __name__ == "__main__":
    unittest.main()
