"""Desk self-assessment + Yahoo→hub bridge hardenings."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from runtime.desk_self_assess import (
    bridge_stale_hub_from_yahoo,
    build_why_idle_payload,
)
from system.market_data_hub import get_market_data_hub
from system.strategy_quality_gate import (
    _close_is_loss,
    consecutive_managed_loss_streak,
)


class DeskSelfAssessTests(unittest.TestCase):
    def test_bridge_publishes_stale_epics(self) -> None:
        hub = get_market_data_hub()
        epic = "IX.D.DOW.IFM.IP"
        hub.publish(
            epic,
            42000.0,
            42001.0,
            source="stale",
            quote_time=time.time() - 120.0,
        )
        # Keep mid near prior publish so packet jump guard does not reject.
        sample = SimpleNamespace(bid=42010.0, offer=42012.0, mid=42011.0)
        with (
            patch(
                "system.market_integrity.effective_entry_quote_budget_sec",
                return_value=10.0,
            ),
            patch(
                "runtime.desk_self_assess._active_epics",
                return_value=[epic],
            ),
            patch(
                "feeder.yahoo_quote_poller.fetch_yahoo_quote",
                return_value=sample,
            ),
        ):
            out = bridge_stale_hub_from_yahoo(force=True)
        self.assertTrue(out.get("ok"))
        self.assertEqual(len(out.get("bridged") or []), 1)
        snap = hub.get_snapshot(epic)
        self.assertIsNotNone(snap)
        self.assertLess(float(snap.age_seconds()), 5.0)

    def test_why_idle_reports_hub_blocker(self) -> None:
        hub = get_market_data_hub()
        epic = "IX.D.DOW.IFM.IP"
        hub.publish(
            epic,
            1.0,
            1.1,
            source="stale",
            quote_time=time.time() - 200.0,
        )
        with (
            patch(
                "system.market_integrity.effective_entry_quote_budget_sec",
                return_value=10.0,
            ),
            patch(
                "runtime.desk_self_assess._active_epics",
                return_value=[epic],
            ),
            patch(
                "system.unified_fulfillment_cache.get_fulfillment_payload",
                return_value={
                    "all_ready": False,
                    "trading_paused": True,
                    "quote_freshness": {
                        "fresh": False,
                        "age_sec": 200.0,
                        "budget_sec": 10.0,
                    },
                },
            ),
            patch(
                "runtime.dual_core_execution.resolve_core_b_gate_stack",
                return_value={
                    "all_clear": False,
                    "gates": [
                        {
                            "gate": 3,
                            "name": "Stream Coupled",
                            "status": "WAITING",
                            "detail": "age=200s",
                        }
                    ],
                },
            ),
            patch(
                "system.strategy_quality_gate.strategy_quality_enabled",
                return_value=False,
            ),
        ):
            payload = build_why_idle_payload()
        self.assertTrue(payload.get("idle"))
        self.assertIsNotNone(payload.get("primary_blocker"))
        self.assertIn(
            payload["primary_blocker"]["id"],
            {"hub_quote_stale", "fulfillment_fail_closed", "gate_stack"},
        )

    def test_fail_safe_null_close_not_loss(self) -> None:
        rec = {
            "won": False,
            "pnl_gbp": 0.0,
            "reason": "broker_upl_hard_floor:broker_upl_null_fail_safe",
        }
        self.assertFalse(_close_is_loss(rec))

    def test_loss_streak_skips_fail_safe(self) -> None:
        closes = [
            {"won": False, "pnl_gbp": 0.0, "reason": "broker_upl_null_fail_safe"},
            {"won": False, "pnl_gbp": 0.0, "reason": "broker_upl_null_fail_safe"},
            {"won": True, "pnl_gbp": 1.0, "reason": "soft_bank"},
        ]
        with patch(
            "runtime.strategy_improvement_tracker.list_managed_closes",
            return_value=closes,
        ):
            self.assertEqual(consecutive_managed_loss_streak(), 0)


if __name__ == "__main__":
    unittest.main()
