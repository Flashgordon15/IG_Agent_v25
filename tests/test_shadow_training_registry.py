"""Tests for shadow_training_registry — IG import isolation from live learning."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.shadow_training_registry import (
    backfill_from_trades,
    count_rows,
    is_shadow_registry_row,
    list_for_ml_training,
    upsert_ig_import,
)
from system.learning_trade_policy import agent_trades_sql_clause, is_agent_learning_row


class ShadowRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db))
        self.store.connect()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_ig_import_row_is_shadow_not_agent_learning(self) -> None:
        row = {
            "setup_key": "IG|imported",
            "source": "ig_import",
            "dry_run": 0,
        }
        self.assertTrue(is_shadow_registry_row(row))
        self.assertFalse(is_agent_learning_row(row))

    def test_ingest_routes_new_ig_transaction_to_shadow_registry(self) -> None:
        ok = self.store.ingest_ig_closed_transaction(
            {
                "deal_reference": "DIAAA123",
                "ig_deal_id": "DIAAA123",
                "market": "Japan 225",
                "epic": "IX.D.NIKKEI.IFM.IP",
                "side": "BUY",
                "entry": 38000.0,
                "exit": 38100.0,
                "size": 1.0,
                "ig_pnl_currency": 12.5,
                "result": "WIN",
                "closed_at": "2026-06-12 10:00:00",
                "notes": "IG transaction history",
            }
        )
        self.assertTrue(ok)
        self.assertEqual(self.store.shadow_training_count(), 1)
        rows = list_for_ml_training(self.store.conn)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_shadow"])
        self.assertEqual(rows[0]["deal_id"], "DIAAA123")

    def test_shadow_rows_excluded_from_agent_trades_clause(self) -> None:
        self.store.ingest_ig_closed_transaction(
            {
                "deal_reference": "DIAAA999",
                "ig_pnl_currency": -5.0,
                "result": "LOSS",
                "closed_at": "2026-06-12 11:00:00",
                "side": "SELL",
                "entry": 100.0,
                "exit": 99.0,
            }
        )
        clause = agent_trades_sql_clause()
        row = self.store.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM trades
            WHERE closed_at IS NOT NULL AND {clause}
            """
        ).fetchone()
        self.assertEqual(int(row["n"] or 0), 0)
        self.assertEqual(count_rows(self.store.conn), 1)

    def test_backfill_copies_existing_ig_import_trades_once(self) -> None:
        self.store.conn.execute(
            """
            INSERT INTO trades(
                opened_at, closed_at, market, epic, side, entry, exit, size,
                stop, target, pnl_points, result, confidence, adjusted_confidence,
                setup_key, dry_run, deal_reference, notes, ig_deal_id, ig_pnl_currency,
                source
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-06-01 09:00:00",
                "2026-06-01 10:00:00",
                "Japan 225",
                "IX.D.NIKKEI.IFM.IP",
                "BUY",
                100.0,
                101.0,
                1.0,
                0.0,
                0.0,
                8.0,
                "WIN",
                0.0,
                0.0,
                "IG|imported",
                0,
                "DIAAAOLD",
                "IG transaction history",
                "DIAAAOLD",
                8.0,
                "ig_import",
            ),
        )
        self.store.conn.commit()
        self.assertEqual(count_rows(self.store.conn), 0)
        copied = backfill_from_trades(self.store.conn)
        self.assertEqual(copied, 1)
        self.assertEqual(backfill_from_trades(self.store.conn), 0)
        self.assertEqual(count_rows(self.store.conn), 1)

    def test_upsert_updates_existing_shadow_row(self) -> None:
        row = {
            "deal_reference": "DIAAAUPD",
            "ig_deal_id": "DIAAAUPD",
            "market": "Japan 225",
            "epic": "IX.D.NIKKEI.IFM.IP",
            "side": "BUY",
            "entry": 38000.0,
            "exit": 38100.0,
            "size": 1.0,
            "ig_pnl_currency": 12.5,
            "result": "WIN",
            "closed_at": "2026-06-12 10:00:00",
            "notes": "IG transaction history",
        }
        self.assertTrue(upsert_ig_import(self.store.conn, row))
        row["exit"] = 38200.0
        row["ig_pnl_currency"] = 18.0
        self.assertTrue(upsert_ig_import(self.store.conn, row))
        saved = self.store.conn.execute(
            "SELECT exit, ig_pnl_currency FROM shadow_training_registry WHERE deal_reference=?",
            ("DIAAAUPD",),
        ).fetchone()
        self.assertIsNotNone(saved)
        self.assertEqual(float(saved["exit"]), 38200.0)
        self.assertEqual(float(saved["ig_pnl_currency"]), 18.0)

    def test_strategy_close_not_mirrored_on_ig_update(self) -> None:
        self.store.conn.execute(
            """
            INSERT INTO trades(
                opened_at, closed_at, market, epic, side, entry, exit, size,
                stop, target, pnl_points, result, confidence, adjusted_confidence,
                setup_key, dry_run, deal_reference, notes, ig_deal_id, ig_pnl_currency,
                source
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-06-12 09:00:00",
                "2026-06-12 10:00:00",
                "Japan 225",
                "IX.D.NIKKEI.IFM.IP",
                "BUY",
                100.0,
                101.0,
                1.0,
                0.0,
                0.0,
                10.0,
                "WIN",
                80.0,
                82.0,
                "BUY|bull|london|atr0-30",
                0,
                "DIAAASTRAT",
                "agent",
                "DIAAASTRAT",
                15.0,
                "strategy",
            ),
        )
        self.store.conn.commit()
        self.store.ingest_ig_closed_transaction(
            {
                "deal_reference": "DIAAASTRAT",
                "ig_pnl_currency": 16.0,
                "result": "WIN",
                "closed_at": "2026-06-12 10:05:00",
            }
        )
        self.assertEqual(self.store.shadow_training_count(), 0)


if __name__ == "__main__":
    unittest.main()
