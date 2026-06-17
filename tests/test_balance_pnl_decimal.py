"""Decimal-safe balance / drawdown P&L resolution."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import system.drawdown_monitor as dm
from system.balance_pnl_decimal import (
    decimal_to_float,
    extract_ig_account_balance_block,
    money_decimal,
    resolve_daily_pnl_gbp_decimal,
    session_pnl_decimal,
)


class BalancePnlDecimalTests(unittest.TestCase):
    def test_money_decimal_from_string_avoids_float_drift(self) -> None:
        d = money_decimal("1000.00")
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d, Decimal("1000.00"))
        self.assertEqual(decimal_to_float(d), 1000.0)

    def test_session_pnl_1000_vs_499_97_available_mismatch(self) -> None:
        """balance=1000, available=499.97 must not produce -500.03 session P&L on balance field."""
        start = money_decimal("1000.00")
        balance = money_decimal("1000.00")
        available = money_decimal("499.97")
        assert start is not None and balance is not None and available is not None

        pnl_balance = session_pnl_decimal(session_start=start, current_balance=balance)
        pnl_available = session_pnl_decimal(session_start=start, current_balance=available)
        assert pnl_balance is not None and pnl_available is not None

        self.assertEqual(pnl_balance, Decimal("0.00"))
        self.assertEqual(pnl_available, Decimal("-500.03"))

    def test_extract_ig_account_balance_block_flags_delta(self) -> None:
        row = {
            "accountId": "ABC",
            "currency": "GBP",
            "balance": {
                "balance": 1000.0,
                "available": 499.97,
                "profitLoss": 0.0,
            },
        }
        parsed = extract_ig_account_balance_block(row)
        self.assertAlmostEqual(parsed["balance"], 1000.0, places=2)
        self.assertAlmostEqual(parsed["available"], 499.97, places=2)
        self.assertAlmostEqual(parsed["balance_available_delta"], 500.03, places=2)

    def test_drawdown_monitor_uses_balance_field_only(self) -> None:
        dm.configure(alert_threshold_pct=999.0)
        dm.reset_session("1000.00", field="balance")
        dm.update("1000.00", field="balance")
        snap = dm.snapshot_decimal_debug()
        self.assertEqual(snap["session_pnl_gbp"], 0.0)
        self.assertEqual(snap["last_balance_field_used"], "balance")

        dm.update("499.97", field="available")
        snap2 = dm.snapshot_decimal_debug()
        self.assertEqual(snap2["last_balance_field_used"], "available")
        # Session start still 1000 — but guard excludes non-balance field from breach math
        self.assertAlmostEqual(snap2["session_pnl_gbp"], -500.03, places=2)

    @patch("system.balance_pnl_decimal.collect_drawdown_debug_context")
    def test_resolve_daily_pnl_prefers_store_over_session_mismatch(
        self, mock_ctx: MagicMock
    ) -> None:
        mock_ctx.return_value = {
            "parsed_primary_account": {
                "balance_decimal": "1000.00",
                "available_decimal": "499.97",
                "balance": 1000.0,
                "available": 499.97,
            },
            "open_positions_count": 0,
        }
        with patch(
            "system.balance_pnl_decimal.LearningStore",
            create=True,
        ), patch(
            "system.daily_loss_policy.effective_daily_pnl",
            return_value=0.0,
        ), patch(
            "intelligence.target_engine.get_target_engine"
        ) as mock_te:
            mock_te.return_value.resolve_open_unrealized_gbp.return_value = 0.0
            dm.configure(alert_threshold_pct=999.0)
            dm.reset_session("1000.00", field="balance")
            dm.update("499.97", field="available")
            total, ctx = resolve_daily_pnl_gbp_decimal()
        self.assertEqual(total, Decimal("0.00"))
        self.assertIn("session_delta_skipped", ctx.get("pnl_components", {}))


if __name__ == "__main__":
    unittest.main()
