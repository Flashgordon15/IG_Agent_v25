"""Closed trades dashboard API — no date window on journal rows."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from api.dashboard_data import get_closed_trades


class TestClosedTradesApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.store = LearningStore(str(self.db))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_closed(self, *, closed_at: str, epic: str = "IX.D.NIKKEI.IFM.IP") -> None:
        self.store.conn.execute(
            """
            INSERT INTO trades (
                opened_at, closed_at, market, epic, side, entry, exit, size,
                pnl_points, result, dry_run, source
            ) VALUES (?, ?, ?, ?, 'BUY', 100, 101, 1, 1.0, 'WIN', 0, 'strategy')
            """,
            (closed_at, closed_at, epic, epic),
        )
        self.store.conn.commit()

    @patch("system.config_loader.ConfigLoader")
    def test_returns_last_n_by_close_time_not_today_only(self, mock_loader) -> None:
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for i in range(12):
            ts = old if i < 5 else recent
            self._insert_closed(closed_at=f"{ts}-{i:02d}")

        cfg = MagicMock()
        cfg.learning_db = str(self.db)
        mock_loader.return_value.load_config.return_value = cfg

        rows = get_closed_trades(limit=10)
        self.assertEqual(len(rows), 10)
        closed_times = [r["closed_at"] for r in rows]
        self.assertTrue(any(str(t).startswith(old[:10]) for t in closed_times))


if __name__ == "__main__":
    unittest.main()
