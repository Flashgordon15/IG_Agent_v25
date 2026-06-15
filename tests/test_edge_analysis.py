"""Edge analysis API tests."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from api.edge_analysis import compute_edge_analysis, reset_edge_analysis_cache_for_tests
from system.config import Config

_TEST_CFG = Config(
    _data={
        "stats_lookback_days": 9999,
        "stats_exclude_pre_fix_date": "",
        "ml_min_rows_for_model": 50,
    }
)


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            opened_at TEXT, closed_at TEXT, market TEXT, epic TEXT,
            side TEXT, entry REAL, exit REAL, size REAL, stop REAL, target REAL,
            pnl_points REAL, result TEXT, dry_run INTEGER, ig_pnl_currency REAL,
            setup_key TEXT, source TEXT
        )
        """
    )
    rows = [
        ("2026-06-10 08:00:00", "2026-06-10 09:00:00", "Gold", "CS.D.CFPGOLD.CFP.IP", "BUY", 100, 110, 1, 95, 115, 10, "WIN", 0, 50.0, "agent|buy", "strategy"),
        ("2026-06-10 10:00:00", "2026-06-10 11:00:00", "Gold", "CS.D.CFPGOLD.CFP.IP", "BUY", 100, 95, 1, 95, 115, -5, "LOSS", 0, -25.0, "agent|buy", "strategy"),
        ("2026-06-10 12:00:00", "2026-06-10 13:00:00", "Gold", "CS.D.CFPGOLD.CFP.IP", "SELL", 100, 90, 1, 105, 85, 10, "WIN", 0, 40.0, "agent|sell", "strategy"),
        ("2026-06-10 14:00:00", "2026-06-10 15:00:00", "SIM", "CS.D.CFPGOLD.CFP.IP", "BUY", 100, 120, 1, 95, 115, 20, "WIN", 1, 999.0, "test_setup", "test"),
        ("2026-06-10 08:30:00", "2026-06-10 09:30:00", "Dow", "IX.D.DOW.IFM.IP", "BUY", 100, 90, 1, 95, 115, -10, "LOSS", 0, -30.0, "agent|buy", "strategy"),
        ("2026-06-09 08:00:00", "2026-06-09 09:00:00", "Gold", "CS.D.CFPGOLD.CFP.IP", "BUY", 100, 110, 1, 95, 115, 10, "WIN", 0, 99.0, "IG_IMPORT", "ig_import"),
    ]
    conn.executemany(
        """
        INSERT INTO trades (
            opened_at, closed_at, market, epic, side, entry, exit, size, stop, target,
            pnl_points, result, dry_run, ig_pnl_currency, setup_key, source
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


class EdgeAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_edge_analysis_cache_for_tests()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        _seed_db(Path(self.tmp.name))

    def test_edge_analysis_excludes_sim_trades(self) -> None:
        with patch("data.ml_training_store.MLTrainingStore") as mock_store:
            mock_store.return_value.record_count.return_value = 14
            mock_store.return_value.record_count_since.return_value = 4
            payload = compute_edge_analysis(self.tmp.name, cfg=_TEST_CFG)
        self.assertEqual(payload["overall"]["total_trades"], 4)

    def test_edge_analysis_win_rate_calculation_correct(self) -> None:
        with patch("data.ml_training_store.MLTrainingStore") as mock_store:
            mock_store.return_value.record_count.return_value = 14
            mock_store.return_value.record_count_since.return_value = 4
            payload = compute_edge_analysis(self.tmp.name, cfg=_TEST_CFG)
        self.assertEqual(payload["overall"]["win_rate"], 0.5)

    def test_edge_analysis_profit_factor_calculation_correct(self) -> None:
        with patch("data.ml_training_store.MLTrainingStore") as mock_store:
            mock_store.return_value.record_count.return_value = 14
            mock_store.return_value.record_count_since.return_value = 4
            payload = compute_edge_analysis(self.tmp.name, cfg=_TEST_CFG)
        self.assertAlmostEqual(payload["overall"]["profit_factor"], 1.64, places=2)

    def test_edge_analysis_by_session_grouping_correct(self) -> None:
        with patch("data.ml_training_store.MLTrainingStore") as mock_store:
            mock_store.return_value.record_count.return_value = 14
            mock_store.return_value.record_count_since.return_value = 4
            payload = compute_edge_analysis(self.tmp.name, cfg=_TEST_CFG)
        sessions = {row["session"]: row for row in payload["by_session"]}
        self.assertIn("london_morning", sessions)
        self.assertGreaterEqual(sessions["london_morning"]["trades"], 2)

    def test_edge_analysis_ml_readiness_calculation(self) -> None:
        with patch("data.ml_training_store.MLTrainingStore") as mock_store:
            mock_store.return_value.record_count.return_value = 14
            mock_store.return_value.record_count_since.return_value = 4
            payload = compute_edge_analysis(self.tmp.name, cfg=_TEST_CFG)
        ml = payload["ml_readiness"]
        self.assertEqual(ml["confirmed_live_trades"], 4)
        self.assertEqual(ml["ml_training_store_rows"], 14)
        self.assertEqual(ml["trades_needed_for_ml"], 50)
        self.assertEqual(ml["percentage_ready"], 8)
        self.assertEqual(ml["scorer_mode"], "interim")
        self.assertIn("Interim Scorer", ml["scorer_label"])

    def test_edge_analysis_excludes_ig_import_and_pre_fix(self) -> None:
        cfg = Config(
            _data={
                "stats_lookback_days": 30,
                "stats_exclude_pre_fix_date": "2026-06-10",
                "ml_min_rows_for_model": 50,
            }
        )
        with patch("data.ml_training_store.MLTrainingStore") as mock_store:
            mock_store.return_value.record_count.return_value = 0
            mock_store.return_value.record_count_since.return_value = 0
            payload = compute_edge_analysis(self.tmp.name, cfg=cfg)
        self.assertEqual(payload["overall"]["total_trades"], 4)
        self.assertEqual(payload["date_range"]["exclude_before"], "2026-06-10")


if __name__ == "__main__":
    unittest.main()
