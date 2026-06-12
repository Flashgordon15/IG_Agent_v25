"""Tests for trading.manual_intervention — admin overrides and drawdown shield."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from execution.position_protect_hub import (
    get_trade_manager,
    register_trade_manager,
    reset_position_protect_hub_for_tests,
)
from trading.manual_intervention import (
    SHIELD_BREACH_KEY,
    SHIELD_BREACH_DAY_KEY,
    daily_max_loss_breached,
    entries_blocked_by_shield,
    force_breakeven_now,
    force_terminate_position,
    refresh_daily_drawdown_shield,
    risk_status,
    shield_threshold_gbp,
)


class ManualInterventionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "learning.db"
        self.store = LearningStore(str(self.db))
        self.store.connect()
        self.cfg = MagicMock()
        self.cfg.get = lambda key, default=None: {
            "manual_intervention": {
                "daily_drawdown_shield_enabled": True,
                "daily_drawdown_shield_gbp": 500,
            }
        }.get(key, default)
        self.cfg.currency_code = "GBP"

    def tearDown(self) -> None:
        reset_position_protect_hub_for_tests()
        self.store.close()
        self.tmp.cleanup()

    def test_shield_trips_on_closed_loss_threshold(self) -> None:
        with patch(
            "system.daily_loss_policy.effective_daily_loss_gbp",
            return_value=550.0,
        ):
            result = refresh_daily_drawdown_shield(self.store, self.cfg)
        self.assertTrue(result["daily_max_loss_breached"])
        self.assertGreaterEqual(result["closed_loss_gbp"], 500.0)
        self.assertTrue(daily_max_loss_breached(self.store))

        blocked, reason = entries_blocked_by_shield(self.store, self.cfg)
        self.assertTrue(blocked)
        self.assertIn("drawdown shield", reason)

    def test_shield_resets_on_new_day(self) -> None:
        self.store.set_runtime_state(SHIELD_BREACH_KEY, "1")
        self.store.set_runtime_state(SHIELD_BREACH_DAY_KEY, "2000-01-01")

        self.assertFalse(daily_max_loss_breached(self.store))
        self.assertEqual(self.store.get_runtime_state(SHIELD_BREACH_KEY), "0")

    def test_force_terminate_position_closes_matching_epic(self) -> None:
        rest = MagicMock()
        rest.open_positions.return_value = [
            {
                "position": {
                    "dealId": "DEAL1",
                    "direction": "BUY",
                    "size": 1.0,
                },
                "market": {"epic": "CS.D.CFPGOLD.CFP.IP"},
            }
        ]
        rest._do_close_position.return_value = {"verified_closed": True}

        result = force_terminate_position(
            "CS.D.CFPGOLD.CFP.IP",
            rest=rest,
            cfg=self.cfg,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["closed_deal_ids"], ["DEAL1"])
        rest._do_close_position.assert_called_once()

    def test_force_breakeven_updates_local_stop_and_syncs(self) -> None:
        mgr = MagicMock()
        trade = MagicMock()
        trade.__getitem__.side_effect = lambda k: {
            "id": 7,
            "side": "BUY",
            "entry": 4200.0,
            "ig_deal_id": "DEAL7",
        }[k]
        trade.keys.return_value = ["id", "side", "entry", "ig_deal_id"]
        mgr.store.active_trades.return_value = [trade]
        mgr._round_stop_level.return_value = 4200.0
        mgr._execute_stop_sync.return_value = True
        register_trade_manager("CS.D.CFPGOLD.CFP.IP", mgr)
        self.assertIsNotNone(get_trade_manager("CS.D.CFPGOLD.CFP.IP"))

        result = force_breakeven_now(
            "CS.D.CFPGOLD.CFP.IP",
            rest=MagicMock(),
            cfg=self.cfg,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        mgr.store.update_stop.assert_called_once()
        mgr._execute_stop_sync.assert_called_once()

    def test_risk_status_includes_shield_and_daily_loss_gate(self) -> None:
        with patch(
            "system.daily_loss_policy.daily_loss_gate_status",
            return_value=(True, "ok", {"tier": "ok"}),
        ):
            payload = risk_status(self.store, self.cfg)
        self.assertTrue(payload["ok"])
        self.assertIn("shield", payload)
        self.assertIn("daily_loss_gate", payload)


if __name__ == "__main__":
    unittest.main()
