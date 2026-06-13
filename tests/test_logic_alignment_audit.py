"""
Logic Alignment Audit — IG Agent v29.1

Covers:
  1. Per-epic cap of 2 blocks a 3rd position on the same epic
  2. Fast protect touch causes slow loop to skip trailing
  3. Entry shields block entries but not stop dispatch enqueue
  4. Scalping exit path not gated by sentiment/fitness
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import Quote, TradeRecord
from execution.correlation_guard import check_open_book_limits
from execution.order_validator import OrderValidator
from execution.protect_priority import (
    mark_fast_protect_touch,
    reset_protect_priority_for_tests,
    slow_loop_should_skip_trailing,
)
from execution.stop_dispatch_worker import (
    StopDispatchJob,
    configure_stop_dispatch,
    configure_sync_mode,
    enqueue_stop_dispatch,
    reset_stop_dispatch_worker_for_tests,
)
from execution.types import TradeSignal
from system.config import Config
from trading.trade_manager import TradeManager


def _cfg(**overrides) -> Config:
    import json

    raw = json.loads((ROOT / "config" / "config_v25.json").read_text())
    raw.update(
        {
            "one_position_per_epic": False,
            "max_open_positions": 5,
            "max_positions_per_epic": 2,
            "trading_hours_enabled": False,
            "market_watch_enabled": False,
            "adaptive_max_entry_spread": 100.0,
            "min_atr_points": 0,
            "max_consecutive_losses": 0,
            "adaptive_block_bad_setups": False,
            "cooldown_seconds": 0,
            "scalping_framework": {"enabled": False},
            "execution_protect": {"enabled": False},
        }
    )
    raw.update(overrides)
    return Config(_data=raw)


def _signal(epic: str = "IX.D.NIKKEI.IFM.IP", conf: float = 85.0) -> TradeSignal:
    return TradeSignal(
        market="Japan 225",
        epic=epic,
        direction="BUY",
        raw_confidence=conf,
        adjusted_confidence=conf,
        setup_key="audit|test",
        quote=Quote(datetime(2026, 6, 13, 12, 0), 38000.0, 38007.0),
        snapshot={},
        notes="logic alignment audit",
    )


def _validator(cfg: Config) -> OrderValidator:
    v = OrderValidator(cfg)
    v.check_session = lambda epic="": (True, "")
    v.check_market_hours = lambda epic: (True, "")
    v.check_circuit_breaker = lambda: (True, "")
    return v


def _open_trade(
    store: LearningStore,
    *,
    epic: str = "IX.D.NIKKEI.IFM.IP",
    entry: float = 100.0,
    stop: float = 90.0,
) -> int:
    return store.open_trade(
        TradeRecord(
            id=None,
            market="Japan 225",
            epic=epic,
            side="BUY",
            entry=entry,
            exit=None,
            size=1.0,
            stop=stop,
            target=entry + 50,
            pnl_points=None,
            result=None,
            confidence=85.0,
            adjusted_confidence=85.0,
            setup_key="audit|test",
            dry_run=True,
            deal_reference="REF-AUDIT",
            notes="audit",
        )
    )


# ---------------------------------------------------------------------------
# Vector 1 — per-epic cap of 2
# ---------------------------------------------------------------------------


class PerEpicCapTests(unittest.TestCase):
    def test_config_v29_overlay_cap_is_two(self) -> None:
        import json

        raw = json.loads((ROOT / "config" / "config_v29.json").read_text())
        self.assertEqual(raw["max_positions_per_epic"], 2)
        self.assertEqual(raw["max_open_positions"], 5)

    def test_order_validator_blocks_third_on_epic(self) -> None:
        cfg = _cfg()
        v = _validator(cfg)
        result = v.validate(
            _signal(),
            open_position_count=lambda epic: 2,
            open_total_count=lambda: 2,
        )
        self.assertFalse(result.allowed)
        self.assertFalse(result.checks["position_limit"])

    def test_trading_loop_risk_gate_blocks_at_cap(self) -> None:
        from tests.test_trading_loop import _make_loop, _quote

        loop = _make_loop()
        loop._config.max_positions_per_epic = 2
        loop._execution_loop.execution_engine.trade_tracker.count_open_for_epic.return_value = 2
        loop._execution_loop.execution_engine.trade_tracker.count_open_total.return_value = 2
        loop._signal_engine.evaluate.return_value = MagicMock(
            signal="BUY",
            adjusted_confidence=90.0,
            setup_key="test",
            snapshot={"atr": 50.0},
        )

        with patch("system.market_data_hub.get_market_data_hub") as hub_mock:
            hub_mock.return_value.normal_spread.return_value = 1.0
            gate = loop._gate_risk_validation(_quote())

        self.assertFalse(gate.passed)
        self.assertIn("max 2", gate.detail)

    def test_correlation_guard_blocks_third_on_epic(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        positions = [
            {"epic": epic, "side": "BUY"},
            {"epic": epic, "side": "BUY"},
        ]
        with patch(
            "execution.correlation_guard._max_positions_per_epic", return_value=2
        ):
            ok, detail = check_open_book_limits(epic, "BUY", positions)
        self.assertFalse(ok)
        self.assertIn("per-epic 2 >= max 2", detail)


# ---------------------------------------------------------------------------
# Vector 2 — fast vs slow protect de-confliction
# ---------------------------------------------------------------------------


class ProtectPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_protect_priority_for_tests()

    def tearDown(self) -> None:
        reset_protect_priority_for_tests()

    def test_fast_touch_skips_slow_trailing(self) -> None:
        mark_fast_protect_touch(42)
        self.assertTrue(slow_loop_should_skip_trailing(42))

    def test_slow_loop_skips_trailing_after_fast_hub_update(self) -> None:
        td = tempfile.mkdtemp()
        store = LearningStore(str(Path(td) / "t.sqlite3"))
        store.connect()
        cfg = _cfg()
        trade_id = _open_trade(store, entry=100.0, stop=90.0)
        mgr = TradeManager(cfg, store, skip_ig_synced_exits=True)
        quote = Quote(datetime(2026, 6, 13, 12, 0), 120.0, 120.5)

        mark_fast_protect_touch(trade_id)

        with patch.object(mgr, "_apply_breakeven") as be_mock, patch.object(
            mgr, "_apply_trailing"
        ) as trail_mock:
            mgr.update_from_quote(
                "Japan 225",
                "IX.D.NIKKEI.IFM.IP",
                quote,
                fast_path=False,
            )
            be_mock.assert_not_called()
            trail_mock.assert_not_called()

        row = store.conn.execute(
            "SELECT stop FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        self.assertEqual(float(row["stop"]), 90.0)


# ---------------------------------------------------------------------------
# Vector 3 — exit path exemption for entry shields
# ---------------------------------------------------------------------------


class ExitShieldExemptionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_stop_dispatch_worker_for_tests()

    def tearDown(self) -> None:
        reset_stop_dispatch_worker_for_tests()

    def test_exits_exempt_policy(self) -> None:
        from trading.manual_intervention import exits_exempt_from_entry_shields

        self.assertTrue(exits_exempt_from_entry_shields())

    def test_shield_blocks_risk_manager_entry(self) -> None:
        from execution.risk_manager import RiskManager

        cfg = _cfg(max_daily_loss_gbp=500)
        store = MagicMock()
        with patch(
            "trading.manual_intervention.entries_blocked_by_shield",
            return_value=(True, "shield tripped"),
        ), patch(
            "system.daily_loss_policy.daily_loss_gate_status",
            return_value=(True, "", {}),
        ):
            rm = RiskManager(cfg, store)
            result = rm.assess(
                direction="BUY",
                execution_params={
                    "size": 1.0,
                    "risk": 40.0,
                    "limit": 80.0,
                    "gate_sourced": True,
                    "spread": 1.0,
                },
            )
        self.assertFalse(result.approved)
        self.assertIn("shield", result.reason)

    def test_shield_does_not_block_stop_dispatch_enqueue(self) -> None:
        dispatched: list[StopDispatchJob] = []

        def handler(job: StopDispatchJob) -> bool:
            dispatched.append(job)
            return True

        configure_sync_mode(True)
        configure_stop_dispatch(handler)

        with patch(
            "trading.manual_intervention.entries_blocked_by_shield",
            return_value=(True, "shield tripped"),
        ):
            job = StopDispatchJob(
                deal_id="DEAL123",
                trade_id=7,
                side="BUY",
                stop=99.5,
                epic="IX.D.NIKKEI.IFM.IP",
                new_limit=None,
            )
            ok = enqueue_stop_dispatch(job)

        self.assertTrue(ok)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].deal_id, "DEAL123")


# ---------------------------------------------------------------------------
# Vector 4 — scalping strategy isolation
# ---------------------------------------------------------------------------


class ScalpingIsolationTests(unittest.TestCase):
    def test_environment_fitness_bypassed_when_scalping_enabled(self) -> None:
        from tests.test_trading_loop import _make_loop, _quote

        loop = _make_loop()
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "enforce_environment_fitness_filter": True,
                "scalping_framework": {"enabled": True},
            }.get(key, default if key != "scalping_framework" else {"enabled": True})
        )
        loop._config.__getitem__ = lambda self, key: loop._config.get(key)
        loop._env.score.return_value = 10.0

        with patch(
            "execution.scalping.config.is_scalping_enabled", return_value=True
        ):
            gate = loop._gate_environment_fitness(_quote())

        self.assertTrue(gate.passed)
        self.assertTrue(gate.value.get("bypass"))

    def test_scalping_exit_management_isolated_helper(self) -> None:
        from execution.scalping.config import is_scalping_exit_management_isolated

        self.assertFalse(is_scalping_exit_management_isolated(_cfg()))
        self.assertTrue(
            is_scalping_exit_management_isolated(
                _cfg(scalping_framework={"enabled": True})
            )
        )

    def test_scalping_exit_runs_without_standard_trailing(self) -> None:
        td = tempfile.mkdtemp()
        store = LearningStore(str(Path(td) / "t.sqlite3"))
        store.connect()
        cfg = _cfg(
            scalping_framework={"enabled": True},
            execution_protect={"enabled": False},
        )
        trade_id = _open_trade(store, entry=100.0, stop=90.0)
        mgr = TradeManager(cfg, store, skip_ig_synced_exits=True)
        quote = Quote(datetime(2026, 6, 13, 12, 0), 115.0, 115.5)

        with patch.object(mgr, "_apply_scalping_breakeven_trail", return_value=[]) as scalp_mock, patch.object(
            mgr, "_apply_trailing"
        ) as trail_mock:
            mgr.update_from_quote(
                "Japan 225",
                "IX.D.NIKKEI.IFM.IP",
                quote,
                fast_path=False,
            )
            scalp_mock.assert_called_once()
            trail_mock.assert_not_called()

    def test_entry_halt_does_not_block_stop_dispatch(self) -> None:
        from execution.scalping.entry_halt import clear_entry_halt_for_tests, halt_entries

        reset_stop_dispatch_worker_for_tests()
        clear_entry_halt_for_tests()
        halt_entries("protection failure test")

        dispatched: list[StopDispatchJob] = []

        def handler(job: StopDispatchJob) -> bool:
            dispatched.append(job)
            return True

        configure_sync_mode(True)
        configure_stop_dispatch(handler)

        job = StopDispatchJob(
            deal_id="DEAL999",
            trade_id=1,
            side="SELL",
            stop=101.0,
            epic="CS.D.EURUSD.CFD.IP",
            new_limit=None,
        )
        ok = enqueue_stop_dispatch(job)

        self.assertTrue(ok)
        self.assertEqual(len(dispatched), 1)
        clear_entry_halt_for_tests()


if __name__ == "__main__":
    unittest.main()
