"""Tests for system.data_exporter — shadow registry CSV audit export."""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.shadow_training_registry import upsert_ig_import
from system.data_exporter import export_shadow_registry_to_csv


class DataExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db))
        self.store.connect()
        upsert_ig_import(
            self.store.conn,
            {
                "deal_reference": "SHADOW1",
                "market": "Spot Gold",
                "epic": "CS.D.CFPGOLD.CFP.IP",
                "side": "BUY",
                "entry": 4200.0,
                "exit": 4210.0,
                "ig_pnl_currency": 25.0,
                "result": "WIN",
                "opened_at": "2026-06-10 14:00:00",
                "closed_at": "2026-06-10 15:00:00",
            },
        )
        upsert_ig_import(
            self.store.conn,
            {
                "deal_reference": "SHADOW2",
                "market": "Spot Gold",
                "epic": "CS.D.CFPGOLD.CFP.IP",
                "side": "SELL",
                "entry": 4210.0,
                "exit": 4205.0,
                "ig_pnl_currency": -10.0,
                "result": "LOSS",
                "opened_at": "2026-06-11 10:00:00",
                "closed_at": "2026-06-11 11:00:00",
            },
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_export_shadow_registry_to_csv_read_only(self) -> None:
        out = Path(self.tmp.name) / "shadow_export.csv"
        result = export_shadow_registry_to_csv(db_path=self.db, output_path=out)

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["row_count"], 2)
        self.assertEqual(result["summary"]["overall_win_rate"], 0.5)
        self.assertTrue(out.is_file())

        rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["epic"], "CS.D.CFPGOLD.CFP.IP")
        self.assertEqual(rows[0]["entry_price"], "4200.0")
        self.assertEqual(rows[0]["exit_price"], "4210.0")
        self.assertEqual(rows[0]["cumulative_win_rate"], "1.0")
        self.assertEqual(rows[1]["cumulative_win_rate"], "0.5")
        self.assertEqual(rows[1]["epic_cumulative_win_rate"], "0.5")

    def test_export_missing_db_raises(self) -> None:
        missing = Path(self.tmp.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            export_shadow_registry_to_csv(db_path=missing)


if __name__ == "__main__":
    unittest.main()
