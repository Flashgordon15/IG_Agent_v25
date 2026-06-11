"""Full-close points scoring via live PointsEngine (get_points_engine)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from execution.ml_training_hooks import configure_ml_training
from trading.points_engine import PointsEngine, set_points_state_path_for_tests


def _insert_closed_trade(
    store: LearningStore,
    *,
    deal_id: str = "DEAL-FC1",
    source: str = "strategy",
    ig_pnl_currency: float | None = None,
) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = store.conn.execute(
        """
        INSERT INTO trades (
            opened_at, closed_at, market, epic, side, entry, exit, size,
            pnl_points, result, confidence, adjusted_confidence, setup_key,
            dry_run, deal_reference, ig_deal_id, source, ig_pnl_currency
        )
        VALUES (?, ?, ?, ?, 'BUY', 100.0, 101.0, 1.0,
                10.0, 'WIN', 90.0, 90.0, 'BUY|bull|asia_early',
                0, ?, ?, ?, ?)
        """,
        (
            now,
            now,
            "Japan 225",
            "IX.D.NIKKEI.IFM.IP",
            deal_id,
            deal_id,
            source,
            ig_pnl_currency,
        ),
    )
    store.conn.commit()
    return int(cur.lastrowid)


class FullClosePointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "points_state.json"
        set_points_state_path_for_tests(self.state_path)
        self.db_path = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db_path))
        self.points = PointsEngine(self.store, state_path=self.state_path)
        configure_ml_training(points_engine=self.points)

    def tearDown(self) -> None:
        configure_ml_training(points_engine=None)
        set_points_state_path_for_tests(None)
        self.store.close()
        self.tmp.cleanup()

    def test_full_close_scores_live_points_engine(self) -> None:
        _insert_closed_trade(self.store, deal_id="DEAL-LIVE")
        before = float(self.points._cumulative)
        ok = self.store.apply_ig_transaction_pnl(
            "DEAL-LIVE",
            "DEAL-LIVE",
            25.0,
            "WIN",
        )
        self.assertTrue(ok)
        self.assertGreater(float(self.points._cumulative), before)

    def test_full_close_skips_when_no_live_engine(self) -> None:
        configure_ml_training(points_engine=None)
        _insert_closed_trade(self.store, deal_id="DEAL-NO-PE")
        with patch("system.engine_log.log_engine") as log_mock:
            ok = self.store.apply_ig_transaction_pnl(
                "DEAL-NO-PE",
                "DEAL-NO-PE",
                10.0,
                "WIN",
            )
        self.assertTrue(ok)
        joined = " ".join(str(c) for c in log_mock.call_args_list)
        self.assertIn("live instance not available", joined)

    def test_full_close_skips_sim_source(self) -> None:
        configure_ml_training(points_engine=self.points)
        _insert_closed_trade(self.store, deal_id="DEAL-SIM", source="sim")
        before = float(self.points._cumulative)
        ok = self.store.apply_ig_transaction_pnl(
            "DEAL-SIM",
            "DEAL-SIM",
            50.0,
            "WIN",
        )
        self.assertTrue(ok)
        self.assertEqual(float(self.points._cumulative), before)


if __name__ == "__main__":
    unittest.main()
