"""Optimization manager — evaluation matrix and parameter tuner tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from simulation.optimization_manager import (
    EvaluationEngine,
    ParameterTuner,
    TestbedLedgerMonitor,
    TuningKnobs,
)


class OptimizationManagerTests(unittest.TestCase):
    def _seed_ledger(self, path: Path, rows: list[tuple]) -> None:
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                """
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY,
                    opened_at TEXT, closed_at TEXT, market TEXT, epic TEXT,
                    side TEXT, entry REAL, exit REAL, size REAL, stop REAL,
                    target REAL, pnl_points REAL, result TEXT, confidence REAL,
                    adjusted_confidence REAL, setup_key TEXT, dry_run INTEGER,
                    deal_reference TEXT, notes TEXT, ig_pnl_currency REAL,
                    ig_deal_id TEXT
                )
                """
            )
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO trades (
                        opened_at, closed_at, epic, side, entry, exit,
                        pnl_points, ig_pnl_currency, result, confidence,
                        setup_key, deal_reference
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
            conn.commit()

    def test_evaluation_matrix_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "testbed_ledger.db"
            state = Path(tmp) / "testbed_state.json"
            self._seed_ledger(
                ledger,
                [
                    ("2026-06-10T08:00:00+00:00", "2026-06-10T08:05:00+00:00",
                     "CS.D.EURUSD.CFD.IP", "BUY", 1.08, 1.09, 10, 10, "WIN", 65,
                     "ema_cross", "D1"),
                    ("2026-06-10T09:00:00+00:00", "2026-06-10T09:05:00+00:00",
                     "CS.D.EURUSD.CFD.IP", "SELL", 1.09, 1.08, 8, 8, "WIN", 70,
                     "ema_cross", "D2"),
                    ("2026-06-10T10:00:00+00:00", "2026-06-10T10:05:00+00:00",
                     "IX.D.DOW.IFM.IP", "BUY", 38500, 38400, -20, -20, "LOSS", 62,
                     "breakout", "D3"),
                ],
            )
            monitor = TestbedLedgerMonitor(ledger, state)
            engine = EvaluationEngine(monitor, target_win_rate=0.70, min_trades=3)
            metrics = engine.evaluate(cycle=1, replay_ticks=100)
            self.assertEqual(metrics.closed_trades, 3)
            self.assertAlmostEqual(metrics.win_rate, 2 / 3, places=3)
            self.assertFalse(metrics.passed)

    def test_parameter_tuner_tightens_on_low_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            analytics = Path(tmp) / "analytics"
            tuner = ParameterTuner(analytics)
            knobs_before = tuner.load_knobs()
            from simulation.optimization_manager import ClosedTradeRow, EvaluationMatrix

            metrics = EvaluationMatrix(
                cycle=1,
                closed_trades=10,
                wins=4,
                losses=6,
                win_rate=0.4,
                max_drawdown_gbp=80,
                avg_slippage_pts=1.5,
                total_pnl_gbp=-20,
                replay_ticks=1000,
                passed=False,
            )
            loss = ClosedTradeRow(
                deal_ref="D1",
                epic="CS.D.EURUSD.CFD.IP",
                side="BUY",
                opened_at="t0",
                closed_at="t1",
                entry=1.0,
                exit=0.9,
                pnl=-10,
                result="LOSS",
                confidence=60,
                setup_key="ema_cross",
            )
            knobs_after = tuner.apply(metrics, [loss], replay_ticks=10_000)
            self.assertIsNotNone(knobs_after)
            assert knobs_after is not None
            self.assertGreater(
                knobs_after.signal_threshold_floor,
                knobs_before.signal_threshold_floor,
            )
            overlay = json.loads((analytics / "optimization_overlay.json").read_text())
            self.assertIn("protective_learning", overlay)


if __name__ == "__main__":
    unittest.main()
