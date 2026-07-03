"""Regression tests for hung-cockpit hot-path bugs (NameError, sqlite3.Row, health)."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.open_position_view import positions_from_store_rows


class HungCockpitHotpathTests(unittest.TestCase):
    def test_positions_from_store_rows_accepts_sqlite_row(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE t (epic TEXT, side TEXT, entry REAL, size REAL, stop REAL, target REAL, notes TEXT)"
        )
        conn.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("IX.D.NIKKEI.IFM.IP", "BUY", 51920.0, 0.5, 51910.0, 51940.0, ""),
        )
        row = conn.execute("SELECT * FROM t").fetchone()
        out = positions_from_store_rows([row], None, point_value_gbp=1.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["epic"], "IX.D.NIKKEI.IFM.IP")
        self.assertEqual(out[0]["side"], "BUY")

    def test_build_health_status_uses_fast_iron_cage_only(self) -> None:
        from api import agent_health

        with patch(
            "system.iron_cage_readiness.fast_iron_cage_status_snapshot",
            return_value={"ok": True, "trade_ready": True, "blockers": []},
        ) as fast:
            with patch(
                "system.iron_cage_readiness.evaluate_iron_cage_readiness",
                side_effect=AssertionError("slow iron_cage evaluate must not run on health path"),
            ):
                body = agent_health.build_health_status()
        fast.assert_called_once()
        self.assertTrue(body.get("trade_ready"))


if __name__ == "__main__":
    unittest.main()
