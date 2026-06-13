"""
E2E smoke — scalping framework through validator → executor → trade manager.

Uses MockIGRest + blocking executor path (no live IG order).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import Quote
from execution.cooldown_tracker import CooldownTracker
from execution.execution_engine import ExecutionEngine
from execution.live_executor import LiveExecutor
from execution.scalping.dynamic_spread_filter import (
    DynamicSpreadFilter,
    reset_spread_filter_for_tests,
)
from execution.scalping.entry_halt import clear_entry_halt_for_tests
from execution.scalping.equity_circuit_breaker import reset_equity_circuit_for_tests
from execution.types import ExecutionMode, TradeSignal
from ig_api.mock_clients import MockIGRest
from system.config import Config
from trading.trade_manager import TradeManager


def _base_cfg_data() -> dict:
    return {
        "operating_mode": "DEMO",
        "account_type": "DEMO",
        "auto_trade_enabled": True,
        "allow_live_trading": False,
        "dry_run": False,
        "trade_size": 0.1,
        "currency_code": "USD",
        "cooldown_seconds": 0,
        "signal_threshold": 50,
        "adaptive_max_entry_spread": 100,
        "max_spread": 100,
        "max_spread_points": 100,
        "adaptive_min_trade_size": 0.01,
        "adaptive_max_trade_size": 5,
        "adaptive_min_risk_points": 10,
        "adaptive_max_risk_points": 100,
        "stop_distance_points": 30,
        "limit_distance_points": 90,
        "reward_multiple": 3.0,
        "risk_points": 30,
        "max_open_positions": 5,
        "max_positions_per_epic": 3,
        "max_consecutive_losses": 0,
        "breakeven_enabled": True,
        "breakeven_trigger_points": 30,
        "breakeven_lock_points": 1,
        "adaptive_trailing_stop_enabled": True,
        "adaptive_trailing_trigger_points": 50,
        "adaptive_trailing_distance_points": 25,
        "trading_hours_enabled": False,
        "market_watch_enabled": False,
        "partial_close_enabled": False,
        "min_atr_points": 0,
        "adaptive_execution_enabled": False,
        "execution_protect": {
            "enabled": True,
            "use_limit_at_touch": True,
        },
        "scalping_framework": {
            "enabled": True,
            "spread_ma_periods": 20,
            "spread_ma_multiplier": 1.5,
            "spread_min_samples": 3,
            "protection_verify_ms": 200,
            "commission_points_per_side": 0.5,
            "breakeven_buffer_points": 2.0,
            "atr_trail_multiplier": 0.5,
            "daily_equity_drawdown_pct": 1.5,
        },
        "learning_demo_mode": {"enabled": False},
    }


def _quote(bid: float = 30000.0, spread: float = 2.0) -> Quote:
    return Quote(
        time=datetime.now(timezone.utc),
        bid=bid,
        offer=bid + spread,
    )


class ScalpingE2ESmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_entry_halt_for_tests()
        reset_spread_filter_for_tests()
        reset_equity_circuit_for_tests()
        self._friday_patch = patch(
            "trading.trade_manager.TradeManager._is_friday_close_window",
            return_value=False,
        )
        self._friday_patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(str(Path(self._tmp.name) / "test.db"))
        self.cfg = Config(_data=_base_cfg_data())
        self.client = MockIGRest(initial_bid=30000.0, initial_offer=30002.0)
        self.client.login()
        self.client.mock.balance = 50_000.0

    def tearDown(self) -> None:
        self._friday_patch.stop()
        self._tmp.cleanup()
        clear_entry_halt_for_tests()
        reset_spread_filter_for_tests()
        reset_equity_circuit_for_tests()

    def _signal(self, *, spread: float = 2.0) -> TradeSignal:
        q = _quote(spread=spread)
        return TradeSignal(
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            direction="BUY",
            raw_confidence=88.0,
            adjusted_confidence=88.0,
            setup_key="test_setup",
            quote=q,
            snapshot={"last": {"atr": 12.0}},
            notes="e2e smoke",
            gate_execution_params={
                "actual_size": 0.1,
                "stop_points": 30.0,
                "limit_points": 90.0,
                "gate_sourced": True,
            },
        )

    def test_e2e_validator_passes_healthy_spread(self) -> None:
        from execution.scalping.dynamic_spread_filter import get_spread_filter

        filt = get_spread_filter()
        for _ in range(5):
            filt.record("IX.D.NIKKEI.IFM.IP", 2.0)

        engine = ExecutionEngine(
            mode=ExecutionMode.DEMO,
            config=self.cfg,
            store=self.store,
            rest_client=self.client,
        )
        vr = engine.validate_only(self._signal())
        self.assertTrue(vr.checks.get("execution_protect_spread", False), vr.reasons)
        self.assertTrue(vr.checks.get("scalping_entry_halt", True), vr.reasons)
        self.assertTrue(vr.checks.get("scalping_equity_circuit", True), vr.reasons)

    def test_e2e_validator_blocks_toxic_spread(self) -> None:
        from execution.scalping.dynamic_spread_filter import get_spread_filter

        filt = get_spread_filter()
        for _ in range(5):
            filt.record("IX.D.NIKKEI.IFM.IP", 2.0)

        engine = ExecutionEngine(
            mode=ExecutionMode.DEMO,
            config=self.cfg,
            store=self.store,
            rest_client=self.client,
        )
        vr = engine.validate_only(self._signal(spread=8.0))
        self.assertFalse(vr.allowed)
        self.assertFalse(vr.checks.get("execution_protect_spread", True))

    def test_e2e_limit_entry_protection_and_trade_open(self) -> None:
        limit_calls: list[dict] = []
        orig_limit = self.client.place_limit_entry_atomic

        def _limit(**kw):
            limit_calls.append(kw)
            return orig_limit(**kw)

        self.client.place_limit_entry_atomic = _limit  # type: ignore[method-assign]

        tm = TradeManager(self.cfg, self.store, rest_client=self.client)
        executor = LiveExecutor(self.cfg, self.client)
        signal = self._signal()
        params = {
            "size": 0.1,
            "risk": 30.0,
            "limit": 90.0,
        }

        with (
            patch("execution.live_executor.try_begin_entry", return_value=True),
            patch("execution.live_executor.clear_entry"),
            patch("execution.live_executor.get_rate_limit_manager") as rl,
            patch("execution.correlation_guard.check_and_record", return_value=(True, "")),
            patch("execution.correlation_guard.confirm_direction_risk"),
        ):
            rl.return_value.check_rest_allowed.return_value = None
            result = executor._execute_order_blocking(
                signal,
                params,
                tm,
                CooldownTracker(0),
                mode=ExecutionMode.DEMO,
            )

        self.assertTrue(result.success, result.rejection_reason)
        self.assertEqual(result.action, "EXECUTED")
        self.assertEqual(len(limit_calls), 1)
        self.assertAlmostEqual(limit_calls[0]["level"], signal.quote.offer)
        self.assertEqual(limit_calls[0]["direction"], "BUY")
        rows = list(self.store.active_trades("IX.D.NIKKEI.IFM.IP"))
        self.assertEqual(len(rows), 1)

    def test_e2e_scalping_breakeven_arms_on_quote_tick(self) -> None:
        tm = TradeManager(self.cfg, self.store, rest_client=self.client)
        q0 = _quote(bid=30000.0, spread=2.0)
        trade_id = tm.open_trade_from_execution(
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            side="BUY",
            quote=q0,
            raw_confidence=88.0,
            adjusted_confidence=88.0,
            setup_key="smoke",
            deal_reference="SMOKE-REF",
            notes="smoke",
            execution={"size": 0.1, "risk": 30, "limit": 90},
            dry_run=True,
            ig_deal_id="D-SMOKE",
        )
        self.store.conn.execute(
            "UPDATE trades SET entry_atr=12.0 WHERE id=?",
            (trade_id,),
        )
        self.store.conn.commit()

        # trigger = spread(2) + commission(1) + buffer(2) = 5 pts profit needed
        q_profit = Quote(
            time=datetime.now(timezone.utc),
            bid=30008.0,
            offer=30010.0,
        )
        msgs = tm.update_from_quote("Japan 225", "IX.D.NIKKEI.IFM.IP", q_profit)
        self.assertTrue(any("EXEC_PROTECT BREAKEVEN" in m for m in msgs))
        stop = self.store.get_stop(trade_id)
        self.assertIsNotNone(stop)
        assert stop is not None
        self.assertGreater(stop, 30000.0)


class ScalpingLiveRoutingSmoke(unittest.TestCase):
    """Optional live DEMO routing check — skipped without credentials."""

    def test_live_demo_routing_when_configured(self) -> None:
        from system.config_loader import ConfigLoader, set_mode
        from system.credentials_loader import try_load_credentials

        try:
            cfg = ConfigLoader().load_config()
        except Exception as exc:
            self.skipTest(f"config load failed: {exc}")

        scalping = cfg.get("scalping_framework") or {}
        if not scalping.get("enabled"):
            self.skipTest("scalping_framework.enabled is false")

        cred_status = try_load_credentials()
        creds = cred_status.credentials
        if creds is None or creds.account_type != "DEMO":
            self.skipTest("no DEMO IG credentials — mock E2E only")

        set_mode("DEMO")
        epic = str(cfg.epic or "IX.D.NIKKEI.IFM.IP")
        try:
            from ig_api.rest_client import IGRestClient

            client = IGRestClient(creds)
            client.login()
            route = client.validate_demo_order_routing(
                epic=epic,
                dry_run=True,
                skip_balance_check=True,
            )
        except Exception as exc:
            self.skipTest(f"live DEMO unavailable: {exc}")

        self.assertTrue(route.get("ok"), route.get("error"))
        self.assertIn("demo-api.ig.com", str(route.get("base_url", "")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
