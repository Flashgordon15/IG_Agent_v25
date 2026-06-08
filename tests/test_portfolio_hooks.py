"""Tests for portfolio envelope live hooks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.portfolio_hooks import (
    record_portfolio_entry_from_signal,
    record_portfolio_exit_for_deal,
    reset_portfolio_hooks_for_tests,
)
from execution.types import TradeSignal
from system.portfolio_envelope import snapshot


class PortfolioHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_portfolio_hooks_for_tests()

    def tearDown(self) -> None:
        reset_portfolio_hooks_for_tests()

    def test_entry_and_exit_updates_envelope(self) -> None:
        signal = TradeSignal(
            market="EUR/USD",
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            quote=MagicMock(mid=1.1, spread=0.0001, time=None),
            raw_confidence=80.0,
            adjusted_confidence=80.0,
            setup_key="BUY|bull",
            notes="",
            snapshot={},
        )
        params = {"size": 1.0, "risk": 10.0}
        config = MagicMock()
        config.get = lambda k, d=None: 1.0 if k == "ig_point_value_gbp" else d

        with patch(
            "system.portfolio_envelope.portfolio_gate_enabled", return_value=True
        ):
            record_portfolio_entry_from_signal("DEAL1", signal, params, config=config)
            snap = snapshot()
            self.assertEqual(snap["concurrent_risk_gbp"], 10.0)
            record_portfolio_exit_for_deal("DEAL1", pnl_gbp=5.0)
            snap2 = snapshot()
            self.assertEqual(snap2["concurrent_risk_gbp"], 0.0)
            self.assertEqual(snap2["daily_pnl_gbp"], 5.0)


if __name__ == "__main__":
    unittest.main()
