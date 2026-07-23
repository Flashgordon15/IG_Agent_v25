"""Tests for session win-rate quality gate."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from data.learning_store import LearningStore
from system.strategy_quality_gate import (
    evaluate_desk_halt_gate,
    evaluate_entry_slot_gate,
    evaluate_session_win_rate_gate,
    rolling_managed_win_rate,
    session_labeled_win_rate,
    session_managed_win_rate,
)


class StrategyQualityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(f"{self.tmp.name}/learning.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_close(self, result: str, epic: str = "IX.D.DOW.IFM.IP") -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.store.conn.execute(
            """
            INSERT INTO trades (
                opened_at, closed_at, market, epic, side, entry, exit, size,
                pnl_points, result, setup_key, dry_run, ig_deal_id
            ) VALUES (?, ?, ?, ?, 'BUY', 100, 101, 0.5, 1.0, ?, 'TEST|buy', 0, ?)
            """,
            (now, now, epic, epic, result, f"DEAL-{result}-{epic}"),
        )
        self.store.conn.commit()

    def test_gate_off_passes(self) -> None:
        passed, detail, _ = evaluate_session_win_rate_gate({"strategy_quality": {"enabled": False}})
        self.assertTrue(passed)
        self.assertIn("off", detail)

    def test_gate_blocks_low_win_rate(self) -> None:
        for _ in range(4):
            self._insert_close("LOSS")
        for _ in range(2):
            self._insert_close("WIN")
        cfg = {
            "strategy_quality": {
                "enabled": True,
                "min_session_win_rate": 0.70,
                "min_labeled_closes_before_gate": 6,
                "use_managed_closes": False,
            },
            "learning_db": f"{self.tmp.name}/learning.db",
        }
        import system.config_loader as cl

        orig = cl.get_config
        cl.get_config = lambda reload=False: cfg  # type: ignore[assignment]
        try:
            wins, losses, total, wr = session_labeled_win_rate(cfg=cfg)
            self.assertEqual(total, 6)
            self.assertLess(wr, 0.70)
            passed, detail, _ = evaluate_session_win_rate_gate(cfg)
            self.assertFalse(passed)
            self.assertIn("<", detail)
        finally:
            cl.get_config = orig  # type: ignore[assignment]

    def test_gate_passes_high_win_rate(self) -> None:
        for _ in range(5):
            self._insert_close("WIN")
        for _ in range(1):
            self._insert_close("LOSS")
        cfg = {
            "strategy_quality": {
                "enabled": True,
                "min_session_win_rate": 0.70,
                "min_labeled_closes_before_gate": 6,
                "use_managed_closes": False,
                "emergency_rolling_wr_floor": 0.0,
                "emergency_session_pnl_gbp": -99999.0,
                "desk_halt_entries": False,
            },
            "learning_db": f"{self.tmp.name}/learning.db",
        }
        import system.config_loader as cl
        import runtime.strategy_improvement_tracker as sit

        orig = cl.get_config
        orig_closes = list(sit._state.closes)
        cl.get_config = lambda reload=False: cfg  # type: ignore[assignment]
        sit._state.closes = []
        try:
            passed, detail, _ = evaluate_session_win_rate_gate(cfg)
            self.assertTrue(passed, detail)
        finally:
            cl.get_config = orig  # type: ignore[assignment]
            sit._state.closes = orig_closes

    def test_gate_blocks_from_managed_closes_when_db_unlabeled(self) -> None:
        """Regression: learning DB had 0 WIN/LOSS labels while desk bled at 5% WR."""
        import runtime.strategy_improvement_tracker as sit

        orig_closes = list(sit._state.closes)
        try:
            now = __import__("time").time()
            sit._state.closes = [
                {
                    "ts": now - i * 30,
                    "epic": "IX.D.DOW.IFM.IP",
                    "pnl_gbp": -2.0,
                    "exit_reason": "soft_loss breach",
                    "won": False,
                }
                for i in range(12)
            ]
            cfg = {
                "strategy_quality": {
                    "enabled": True,
                    "min_session_win_rate": 0.70,
                    "min_labeled_closes_before_gate": 6,
                    "use_managed_closes": True,
                    "rolling_min_sample": 8,
                    "rolling_win_rate_floor": 0.35,
                    "loss_streak_pause": 5,
                    "desk_halt_entries": False,
                    "emergency_rolling_wr_floor": 0.0,
                },
                "learning_db": f"{self.tmp.name}/learning.db",
            }
            db_w, db_l, db_t, _ = session_labeled_win_rate(cfg=cfg)
            self.assertEqual(db_t, 0)
            m_w, m_l, m_t, m_wr = session_managed_win_rate(cfg=cfg)
            self.assertGreaterEqual(m_t, 6)
            self.assertLess(m_wr, 0.35)
            passed, detail, value = evaluate_session_win_rate_gate(cfg)
            self.assertFalse(passed)
            self.assertTrue(
                "rolling WR" in detail
                or "loss_streak" in detail
                or "<" in detail
            )
            if value.get("source") is not None:
                self.assertEqual(value.get("source"), "managed_closes")
        finally:
            sit._state.closes = orig_closes

    def test_entry_slot_gate_blocks_outside_allowed(self) -> None:
        cfg = {
            "intraday_slots": {
                "enabled": True,
                "timezone": "Europe/London",
                "entry_allowed_slots": ["europe_open", "us_cash"],
                "slots": [
                    {"id": "overnight", "label": "Overnight", "start": "21:00", "end": "06:00"},
                    {"id": "europe_open", "label": "Europe Open", "start": "08:00", "end": "09:30"},
                ],
            }
        }
        with patch(
            "runtime.intraday_slot_tracker.slot_id_for_timestamp",
            return_value="overnight",
        ):
            passed, detail = evaluate_entry_slot_gate(cfg)
            self.assertFalse(passed)
            self.assertIn("not in entry_allowed_slots", detail)

    def test_entry_slot_gate_allows_configured_slot(self) -> None:
        cfg = {
            "intraday_slots": {
                "enabled": True,
                "timezone": "Europe/London",
                "entry_allowed_slots": ["europe_open", "us_cash"],
                "slots": [
                    {"id": "europe_open", "label": "Europe Open", "start": "08:00", "end": "09:30"},
                ],
            }
        }
        with patch(
            "runtime.intraday_slot_tracker.slot_id_for_timestamp",
            return_value="europe_open",
        ):
            passed, detail = evaluate_entry_slot_gate(cfg)
            self.assertTrue(passed)
            self.assertIn("allowed", detail)

    def test_desk_halt_manual_flag_blocks(self) -> None:
        passed, detail, value = evaluate_desk_halt_gate(
            {"strategy_quality": {"desk_halt_entries": True}}
        )
        self.assertFalse(passed)
        self.assertIn("desk_halt_entries flag active", detail)
        self.assertTrue(value.get("desk_halt_entries"))

    def test_desk_halt_emergency_wr_blocks(self) -> None:
        import runtime.strategy_improvement_tracker as sit

        orig_closes = list(sit._state.closes)
        try:
            now = __import__("time").time()
            sit._state.closes = [
                {
                    "ts": now - i * 30,
                    "epic": "IX.D.DOW.IFM.IP",
                    "pnl_gbp": -2.0,
                    "exit_reason": "soft_loss breach",
                    "won": False,
                }
                for i in range(10)
            ]
            cfg = {
                "strategy_quality": {
                    "desk_halt_entries": False,
                    "emergency_rolling_wr_floor": 0.20,
                    "rolling_window": 20,
                    "rolling_min_sample": 8,
                }
            }
            passed, detail, _ = evaluate_desk_halt_gate(cfg)
            self.assertFalse(passed)
            self.assertIn("emergency desk halt rolling WR", detail)
        finally:
            sit._state.closes = orig_closes

    def test_demo_throughput_disables_emergency_wr_halt(self) -> None:
        import runtime.strategy_improvement_tracker as sit

        orig_closes = list(sit._state.closes)
        try:
            now = __import__("time").time()
            sit._state.closes = [
                {
                    "ts": now - i * 30,
                    "epic": "IX.D.DOW.IFM.IP",
                    "pnl_gbp": -2.0,
                    "exit_reason": "soft_loss breach",
                    "won": False,
                }
                for i in range(10)
            ]
            # Poison session PnL outlier (broker_upl_hard_floor style) must not
            # false-halt under demo soak even if config still says -£500.
            sit._state.closes.append(
                {
                    "ts": now,
                    "epic": "IX.D.DOW.IFM.IP",
                    "pnl_gbp": -5082.58,
                    "exit_reason": "micro_gbp_exit:broker_upl_hard_floor -5082.58 <= -100.00",
                    "won": False,
                }
            )
            cfg = {
                "demo_throughput_mode": {"enabled": True},
                "strategy_quality": {
                    "desk_halt_entries": False,
                    # Stale/missing key must not re-arm WR/PnL freeze under demo soak.
                    "emergency_rolling_wr_floor": 0.20,
                    "rolling_window": 20,
                    "rolling_min_sample": 8,
                    "emergency_session_pnl_gbp": -500.0,
                },
            }
            passed, detail, value = evaluate_desk_halt_gate(cfg)
            self.assertTrue(passed, detail)
            self.assertEqual(value.get("emergency_rolling_wr_floor"), 0.0)
            self.assertEqual(value.get("emergency_session_pnl_gbp"), 0.0)
            self.assertTrue(value.get("demo_throughput_soak"))
        finally:
            sit._state.closes = orig_closes

    def test_desk_halt_emergency_session_pnl_blocks_when_not_demo(self) -> None:
        import runtime.strategy_improvement_tracker as sit

        orig_closes = list(sit._state.closes)
        try:
            now = __import__("time").time()
            sit._state.closes = [
                {
                    "ts": now,
                    "epic": "IX.D.DOW.IFM.IP",
                    "pnl_gbp": -250.0,
                    "exit_reason": "soft_loss breach",
                    "won": False,
                }
            ]
            cfg = {
                "demo_throughput_mode": {"enabled": False},
                "strategy_quality": {
                    "desk_halt_entries": False,
                    "emergency_rolling_wr_floor": 0.0,
                    "emergency_session_pnl_gbp": -200.0,
                },
            }
            passed, detail, _ = evaluate_desk_halt_gate(cfg)
            self.assertFalse(passed)
            self.assertIn("emergency desk halt session PnL", detail)
        finally:
            sit._state.closes = orig_closes


if __name__ == "__main__":
    unittest.main()
