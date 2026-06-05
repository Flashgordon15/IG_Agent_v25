"""Unit tests for trade eligibility countdown helpers."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.session_manager import COLD_START_BARS
from trading.trade_eligibility import (
    build_trade_eligibility,
    cold_start_remaining_sec,
    format_duration_display,
    next_bar_close_at,
    seconds_until_bar_close,
)
from trading.trading_loop import GateResult


class TradeEligibilityHelperTests(unittest.TestCase):
    def test_next_bar_close_at_on_boundary(self) -> None:
        dt = datetime(2026, 5, 27, 10, 17, 30)
        nxt = next_bar_close_at(dt)
        self.assertEqual(nxt, datetime(2026, 5, 27, 10, 20, 0))

    def test_seconds_until_bar_close(self) -> None:
        dt = datetime(2026, 5, 27, 10, 17, 0)
        self.assertEqual(seconds_until_bar_close(dt), 180.0)

    def test_format_duration_display(self) -> None:
        self.assertEqual(format_duration_display(90), "~1:30")
        self.assertEqual(format_duration_display(20 * 60), "~20 min")

    def test_cold_start_remaining_sec(self) -> None:
        session = MagicMock()
        session.bars_since_open.return_value = 2
        rem = cold_start_remaining_sec(session)
        self.assertEqual(rem, (COLD_START_BARS - 2) * 5 * 60)

    def test_build_cold_start_from_gates(self) -> None:
        session = MagicMock()
        session.is_cold_start.return_value = True
        session.bars_since_open.return_value = 1
        session.is_session_open.return_value = True

        gates = [
            GateResult(name="session_open", passed=True, value=True, detail="open"),
            GateResult(
                name="cold_start_gap",
                passed=False,
                value={"cold": True, "gap": False, "bars": 1},
                detail="cold",
            ),
        ]
        out = build_trade_eligibility(
            gates=gates,
            session=session,
            signal_engine=None,
            market="japan",
            epic="IX.D.NIKKEI.IFM.IP",
            block_reason="",
            sig=None,
            now=datetime(2026, 5, 27, 10, 0),
        )
        self.assertIsNotNone(out)
        assert out is not None
        from trading.session_manager import COLD_START_BARS

        self.assertEqual(out.kind, "cold_start")
        self.assertIn(f"1/{COLD_START_BARS}", out.display)

    def test_build_score_block_no_timer(self) -> None:
        session = MagicMock()
        session.is_cold_start.return_value = False
        gates = [
            GateResult(name="session_open", passed=True, value=True, detail="open"),
            GateResult(name="cold_start_gap", passed=True, value={}, detail="ok"),
            GateResult(
                name="environment_fitness",
                passed=False,
                value={"score": 30},
                detail="fitness 30%",
            ),
        ]
        out = build_trade_eligibility(
            gates=gates,
            session=session,
            signal_engine=None,
            market="japan",
            epic="IX.D.NIKKEI.IFM.IP",
            block_reason="",
            sig=None,
            now=datetime(2026, 5, 27, 10, 0),
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.kind, "blocked")
        self.assertIn("no timer", out.display)


if __name__ == "__main__":
    unittest.main()
