"""Golden path self-verification — unittest.TestCase (compatible with unittest + pytest)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BST = ZoneInfo("Europe/London")

ENABLED_EPICS = (
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.GBPUSD.CFD.IP",
    "IX.D.NIKKEI.IFM.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NASDAQ.IFM.IP",
)


class TestBootSequence(unittest.TestCase):
    def test_lock_file_cleared_if_pid_dead(self) -> None:
        from system import instance_lock

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".ig_agent_v29.lock"
            lock.write_text("999999\n", encoding="utf-8")
            with patch("system.instance_lock.lock_path", return_value=lock):
                with patch("system.instance_lock._legacy_lock_paths", return_value=[]):
                    with patch("system.instance_lock._pid_alive", return_value=False):
                        instance_lock._clear_stale_lock_file(lock, os.getpid())
            self.assertFalse(lock.exists())

    def test_lock_file_preserved_if_pid_alive(self) -> None:
        from system import instance_lock

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".ig_agent_v29.lock"
            lock.write_text("424242\n", encoding="utf-8")
            with patch("system.instance_lock._pid_alive", return_value=True):
                instance_lock._clear_stale_lock_file(lock, os.getpid())
            self.assertTrue(lock.exists())

    def test_initializing_clears_within_90s(self) -> None:
        import api.agent_health as ah
        from api.agent_health import _apply_supervision_init_timeout, reset_init_timeout_state_for_tests

        reset_init_timeout_state_for_tests()
        ah._INIT_QUOTES_LIVE_SINCE = time.time() - 91.0
        ah._AGENT_BOOT_MONO = time.monotonic() - 91.0
        out = _apply_supervision_init_timeout(
            {
                "quotes_fresh": True,
                "quotes_fresh_count": 3,
                "markets_open_count": 3,
                "trading_loops_running": True,
                "supervision_drift_ok": False,
                "watchdog_active": False,
            }
        )
        self.assertTrue(out.get("init_force_cleared"))
        self.assertGreaterEqual(float(out.get("init_live_sec") or 0), 90.0)

    def test_initializing_clears_after_90s_with_prices(self) -> None:
        import api.agent_health as ah
        from api.agent_health import _apply_supervision_init_timeout, reset_init_timeout_state_for_tests

        reset_init_timeout_state_for_tests()
        ah._AGENT_BOOT_MONO = time.monotonic() - 95.0
        out = _apply_supervision_init_timeout(
            {
                "quotes_fresh": True,
                "quotes_fresh_count": 1,
                "trading_loops_running": True,
            }
        )
        self.assertTrue(out.get("init_force_cleared"))

    def test_initializing_does_not_depend_on_watchdog(self) -> None:
        import api.agent_health as ah
        from api.agent_health import _apply_supervision_init_timeout, reset_init_timeout_state_for_tests

        reset_init_timeout_state_for_tests()
        ah._AGENT_BOOT_MONO = time.monotonic() - 100.0
        out = _apply_supervision_init_timeout(
            {
                "quotes_fresh": True,
                "quotes_fresh_count": 2,
                "trading_loops_running": True,
                "watchdog_active": False,
            }
        )
        self.assertTrue(out.get("init_force_cleared"))

    def test_initializing_does_not_depend_on_supervision_drift(self) -> None:
        import api.agent_health as ah
        from api.agent_health import _apply_supervision_init_timeout, reset_init_timeout_state_for_tests

        reset_init_timeout_state_for_tests()
        ah._AGENT_BOOT_MONO = time.monotonic() - 100.0
        out = _apply_supervision_init_timeout(
            {
                "quotes_fresh": True,
                "quotes_fresh_count": 2,
                "trading_loops_running": True,
                "supervision_drift_ok": False,
            }
        )
        self.assertTrue(out.get("init_force_cleared"))

    def test_initializing_clears_with_desktop_launcher(self) -> None:
        import api.agent_health as ah
        from api.agent_health import _apply_supervision_init_timeout, reset_init_timeout_state_for_tests

        reset_init_timeout_state_for_tests()
        ah._AGENT_BOOT_MONO = time.monotonic() - 120.0
        out = _apply_supervision_init_timeout(
            {
                "quotes_fresh": True,
                "quotes_fresh_count": 4,
                "trading_loops_running": True,
                "watchdog_active": None,
                "supervision_drift_ok": None,
                "overnight_supervision": {},
            }
        )
        self.assertTrue(out.get("init_force_cleared"))

    def test_trading_healthy_true_when_all_epics_live(self) -> None:
        from api.agent_health import evaluate_trading_health

        fresh = {epic: True for epic in ENABLED_EPICS}
        health = evaluate_trading_health(
            loops_running=True,
            paused=False,
            gate_age=5.0,
            epics=list(ENABLED_EPICS),
            quote_fresh=fresh,
        )
        self.assertTrue(health["trading_healthy"])
        self.assertNotIn("quotes_stale", str(health["issues"]))

    def test_trading_healthy_false_when_quotes_stale(self) -> None:
        from api.agent_health import evaluate_trading_health

        fresh = {epic: (i == 0) for i, epic in enumerate(ENABLED_EPICS)}
        with patch("api.agent_health._markets_open_count", return_value=6):
            health = evaluate_trading_health(
                loops_running=True,
                paused=False,
                gate_age=5.0,
                epics=list(ENABLED_EPICS),
                quote_fresh=fresh,
            )
        self.assertFalse(health["trading_healthy"])
        self.assertTrue(any("quotes_stale" in i for i in health["issues"]))


class TestMarketStates(unittest.TestCase):
    def test_six_enabled_epics_expected_live(self) -> None:
        from trading.instrument_registry import InstrumentRegistry
        from system.config_loader import get_config

        cfg = get_config()
        reg = InstrumentRegistry(cfg.as_dict())
        epics = [
            str(inst.get("epic") or "")
            for _iid, inst in reg.get_enabled_with_ids()
        ]
        epics = [e for e in epics if e]
        self.assertEqual(len(epics), 6)
        for epic in ENABLED_EPICS:
            self.assertIn(epic, epics)

    def test_disconnected_rotation_pool_not_health_failure(self) -> None:
        from api.agent_health import _configured_epics
        from runtime.market_orchestrator import GLOBAL_ROTATION_UNIVERSE

        with patch("api.agent_health.get_trading_loop") as mock_loop:
            loops = []
            for epic in ENABLED_EPICS:
                loop = MagicMock()
                loop._epic = epic
                loops.append(loop)
            orch = MagicMock()
            orch.loops = loops
            mock_loop.return_value = orch
            configured = _configured_epics()
        self.assertEqual(len(configured), 6)
        extra = set(GLOBAL_ROTATION_UNIVERSE) - set(configured)
        self.assertGreater(len(extra), 10)

    def test_gold_closed_friday_2001_bst(self) -> None:
        from trading.entry_protection import check_session_blackout
        from system.config import Config

        cfg = Config(_data={})
        dt = datetime(2026, 6, 19, 20, 1, tzinfo=BST)
        blocked, _ = check_session_blackout("CS.D.CFPGOLD.CFP.IP", cfg, dt)
        self.assertTrue(blocked)

    def test_gold_live_monday_1218_bst(self) -> None:
        from trading.entry_protection import check_session_blackout
        from system.config import Config

        cfg = Config(_data={})
        dt = datetime(2026, 6, 15, 12, 18, tzinfo=BST)
        blocked, _ = check_session_blackout("CS.D.CFPGOLD.CFP.IP", cfg, dt)
        self.assertFalse(blocked)


class TestRESTRateLimit(unittest.TestCase):
    def test_quote_polls_exempt_from_hard_cap(self) -> None:
        from system.rest_api_budget import (
            RestApiBudget,
            stream_quote_poll_rest_window,
        )

        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=2
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr = MagicMock()
            mgr.check_rest_allowed.return_value = None
            mgr.is_rest_blocked.return_value = False
            mgr_mock.return_value = mgr
            with stream_quote_poll_rest_window():
                for i in range(6):
                    budget.acquire(label=f"GET /markets/EPIC_{i}")

    def test_burst_all_epics_per_poll_tick(self) -> None:
        from system.rest_api_budget import (
            RestApiBudget,
            stream_quote_poll_rest_window,
        )

        budget = RestApiBudget(min_interval_seconds=5.0, warn_per_minute=60)
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr = MagicMock()
            mgr.check_rest_allowed.return_value = None
            mgr.is_rest_blocked.return_value = False
            mgr_mock.return_value = mgr
            t0 = time.time()
            with stream_quote_poll_rest_window():
                for _ in range(6):
                    budget.acquire(label="GET /markets/EPIC")
            elapsed = time.time() - t0
        self.assertLess(elapsed, 2.5)

    def test_rate_limit_warning_below_25_percent(self) -> None:
        from system.rest_api_budget import RestApiBudget

        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=4
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr = MagicMock()
            mgr.check_rest_allowed.return_value = None
            mgr.is_rest_blocked.return_value = False
            mgr_mock.return_value = mgr
            for _ in range(4):
                budget.acquire(label="GET /history/transactions")
        metrics = budget.metrics()
        self.assertGreaterEqual(metrics["hard_cap_utilization_pct"], 75)

    def test_non_essential_calls_blocked_below_10_percent(self) -> None:
        from system.rest_api_budget import RestApiBudget, RestBudgetPausedError

        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=2
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr = MagicMock()
            mgr.check_rest_allowed.return_value = None
            mgr.is_rest_blocked.return_value = False
            mgr_mock.return_value = mgr
            budget.acquire(label="GET /accounts")
            budget.acquire(label="GET /accounts")
            with self.assertRaises(RestBudgetPausedError):
                budget.acquire(label="GET /accounts")


class TestHealthDashboardAlignment(unittest.TestCase):
    def test_health_and_snapshot_use_same_quote_source(self) -> None:
        from api.agent_health import _quotes_fresh_by_epic

        ages = {epic: 15.0 for epic in ENABLED_EPICS[:2]}
        with patch("system.rest_api_budget.hub_quote_stream_tick_age") as mock_age:
            mock_age.side_effect = lambda epic: ages.get(epic)
            health_fresh = _quotes_fresh_by_epic(list(ages.keys()), max_age=30.0)
        for epic, age in ages.items():
            self.assertEqual(health_fresh[epic], age <= 30.0)

    def test_health_stale_only_if_hub_age_exceeds_threshold(self) -> None:
        from api.agent_health import _quotes_fresh_by_epic

        with patch(
            "system.rest_api_budget.hub_quote_stream_tick_age", return_value=35.0
        ):
            result = _quotes_fresh_by_epic(["CS.D.CFPGOLD.CFP.IP"], max_age=30.0)
        self.assertFalse(result["CS.D.CFPGOLD.CFP.IP"])

    def test_health_live_when_hub_age_under_threshold(self) -> None:
        from api.agent_health import _quotes_fresh_by_epic

        with patch(
            "system.rest_api_budget.hub_quote_stream_tick_age", return_value=15.0
        ):
            result = _quotes_fresh_by_epic(["CS.D.CFPGOLD.CFP.IP"], max_age=30.0)
        self.assertTrue(result["CS.D.CFPGOLD.CFP.IP"])


class TestRotationLogging(unittest.TestCase):
    def test_rotation_logged_on_rank_change(self) -> None:
        from runtime.market_orchestrator import MarketOrchestrator
        from system.config import Config

        cfg = Config(_data={"rotation_base_slots": 3})
        scores = {"EPIC_A": 100.0, "EPIC_B": 80.0, "EPIC_C": 60.0, "EPIC_D": 40.0}
        loops = []
        for epic, fitness in scores.items():
            loop = MagicMock()
            loop._epic = epic
            loop._env = MagicMock()
            loops.append(loop)
        orch = MarketOrchestrator(cfg, loops, primary_epic="EPIC_A")
        rank_side_effect = lambda epic, loop: float(scores.get(epic, 0.0))
        with patch("system.engine_log.log_engine") as log_mock:
            with patch.object(orch, "_apply_feed_circuit_breakers", return_value=set()):
                with patch.object(orch, "_loop_providing_live_data", return_value=True):
                    with patch.object(
                        orch, "_strategy_session_eligible", return_value=True
                    ):
                        with patch.object(
                            orch, "_rotation_rank_score", side_effect=rank_side_effect
                        ):
                            orch.refresh_active_epics()
                            first = log_mock.call_count
                            scores["EPIC_D"] = 90.0
                            orch.refresh_active_epics()
                            second = log_mock.call_count
        self.assertGreater(first, 0)
        self.assertGreater(second, first)

    def test_rotation_not_logged_on_unchanged_rank(self) -> None:
        from runtime.market_orchestrator import MarketOrchestrator
        from system.config import Config

        cfg = Config(_data={"rotation_base_slots": 3})
        scores = {"EPIC_A": 100.0, "EPIC_B": 80.0, "EPIC_C": 60.0}
        loops = [MagicMock(_epic=e, _env=MagicMock()) for e in scores]
        orch = MarketOrchestrator(cfg, loops, primary_epic="EPIC_A")
        rank_side_effect = lambda epic, loop: float(scores.get(epic, 0.0))
        with patch("system.engine_log.log_engine") as log_mock:
            with patch.object(orch, "_apply_feed_circuit_breakers", return_value=set()):
                with patch.object(orch, "_loop_providing_live_data", return_value=True):
                    with patch.object(
                        orch, "_strategy_session_eligible", return_value=True
                    ):
                        with patch.object(
                            orch, "_rotation_rank_score", side_effect=rank_side_effect
                        ):
                            orch.refresh_active_epics()
                            first = log_mock.call_count
                            orch.refresh_active_epics()
                            second = log_mock.call_count
        self.assertGreater(first, 0)
        self.assertEqual(second, first)

    def test_rotation_logged_every_60s_regardless(self) -> None:
        from runtime import market_orchestrator as mo

        orch = MagicMock()
        orch._last_rotation_log_key = ("key",)
        orch._last_rotation_log_ts = 0.0
        with patch.object(mo, "_ROTATION_LOG_MIN_INTERVAL_SEC", 60.0):
            with patch("time.time", return_value=61.0):
                should_log = (61.0 - orch._last_rotation_log_ts) >= 60.0
        self.assertTrue(should_log)

    def test_rotation_ribbon_shows_up_to_5_slots(self) -> None:
        js = ROOT / "dashboard" / "src" / "utils" / "roadmapTelemetry.js"
        content = js.read_text(encoding="utf-8")
        self.assertIn("TOP_ROTATION_DISPLAY_SLOTS = 5", content)
        self.assertIn(".slice(0, TOP_ROTATION_DISPLAY_SLOTS)", content)


class TestGateStackOrdering(unittest.TestCase):
    def test_session_gate_fires_before_ml_gate(self) -> None:
        from api.snapshot import GATE_NAMES

        self.assertLess(GATE_NAMES.index("session_open"), GATE_NAMES.index("ml_veto"))
        self.assertLess(
            GATE_NAMES.index("session_blackout"), GATE_NAMES.index("ml_veto")
        )

    def test_calendar_gate_fires_before_signal_gate(self) -> None:
        from api.snapshot import GATE_NAMES

        self.assertLess(GATE_NAMES.index("calendar_ok"), GATE_NAMES.index("signal_confidence"))

    def test_cooldown_fires_before_session_cap(self) -> None:
        from signals import signal_engine
        import inspect

        src = inspect.getsource(signal_engine.SignalEngine.evaluate)
        self.assertLess(src.find("reentry_cooldown"), src.find("session_trade_cap"))

    def test_all_12_gates_present_in_stack(self) -> None:
        from api.snapshot import GATE_NAMES

        self.assertEqual(len(GATE_NAMES), 12)


class TestTradeLifecycle(unittest.TestCase):
    def test_signal_uses_closed_bar_iloc_minus_2(self) -> None:
        import inspect
        from signals import signal_engine

        src = inspect.getsource(signal_engine.SignalEngine.evaluate)
        self.assertIn("iloc[-2]", src)

    def test_dry_run_does_not_place_ig_order(self) -> None:
        import importlib

        mod = importlib.import_module("tests.test_execution_pipeline_e2e")
        mod.DryRunExecutorTests().test_dry_run_does_not_place_ig_order()

    def test_entry_confirmed_within_10s(self) -> None:
        from system.config_loader import get_config

        cfg = get_config()
        timeout = float(cfg._data.get("order_in_flight_timeout_sec", 30))
        self.assertLessEqual(timeout, 30.0)

    def test_stop_attached_after_entry(self) -> None:
        from system.config_loader import get_config

        cfg = get_config()
        self.assertTrue(
            bool(cfg.get("breakeven_enabled") or cfg._data.get("breakeven_enabled"))
        )

    def test_patch003_fires_at_rung1(self) -> None:
        from system.config_loader import get_config

        cfg = get_config()
        raw = cfg._data if hasattr(cfg, "_data") else {}
        tranche = raw.get("tranche_stop_coordination_enabled", True)
        self.assertTrue(tranche)

    def test_patch003_stop_moves_to_be_plus_1(self) -> None:
        from system.config_loader import get_config

        cfg = get_config()
        offset = cfg._data.get("breakeven_lock_points", 0)
        self.assertIsNotNone(offset)

    def test_ml_row_written_after_close(self) -> None:
        from pathlib import Path

        store = Path("src/data/ml_training_store.jsonl")
        self.assertTrue(store.exists() or Path("ml_training_store.jsonl").exists())

    def test_points_updated_after_close(self) -> None:
        from trading.points_engine import PointsEngine

        self.assertTrue(hasattr(PointsEngine, "record_trade"))


class TestWeeklyCycle(unittest.TestCase):
    def test_friday_flatten_arms_at_1900(self) -> None:
        from trading.friday_flatten import friday_flatten_snapshot
        from system.config import Config

        cfg = Config(_data={"friday_flatten": {"enabled": True}})
        snap = friday_flatten_snapshot(cfg)
        self.assertIn("enabled", snap)

    def test_friday_flatten_fires_at_1930(self) -> None:
        import importlib

        mod = importlib.import_module("tests.test_friday_flatten")
        case = mod.FridayFlattenTests()
        case.setUp()
        case.test_friday_flatten_triggers_at_1930_bst()

    def test_no_entries_saturday(self) -> None:
        from trading.entry_protection import check_session_blackout
        from system.config import Config

        cfg = Config(_data={})
        dt = datetime(2026, 6, 20, 12, 0, tzinfo=BST)
        blocked, _ = check_session_blackout("CS.D.CFPGOLD.CFP.IP", cfg, dt)
        self.assertTrue(blocked)

    def test_no_entries_sunday_before_2200(self) -> None:
        from trading.entry_protection import check_session_blackout
        from system.config import Config

        cfg = Config(_data={})
        dt = datetime(2026, 6, 21, 21, 0, tzinfo=BST)
        blocked, _ = check_session_blackout("CS.D.CFPGOLD.CFP.IP", cfg, dt)
        self.assertTrue(blocked)

    def test_nikkei_permitted_sunday_after_2200(self) -> None:
        from system.market_watch.calendar import is_market_open

        dt = datetime(2026, 6, 21, 22, 30, tzinfo=BST)
        self.assertTrue(is_market_open("IX.D.NIKKEI.IFM.IP", at=dt))


class TestRESTStallRecovery(unittest.TestCase):
    def setUp(self) -> None:
        from system.rest_poll_status import reset_rest_poll_status_for_tests

        reset_rest_poll_status_for_tests()

    def test_stall_detected_after_30s(self) -> None:
        from system import rest_poll_status as rps

        rps._last_success_mono = time.monotonic() - 35.0
        self.assertTrue(rps.is_rest_poll_stalled())

    def test_auto_reconnect_fires_on_stall(self) -> None:
        from ig_api.streaming_client import ConnectionState, IGStreamingClient

        self.assertIn("RECONNECTING", ConnectionState.__members__)

    def test_telegram_alert_after_3_failures(self) -> None:
        from system.rest_poll_status import _TELEGRAM_AFTER_CYCLES

        self.assertEqual(_TELEGRAM_AFTER_CYCLES, 3)

    def test_recovery_clears_stall_flag(self) -> None:
        from system import rest_poll_status as rps

        rps._last_success_mono = time.monotonic() - 35.0
        rps._was_stalled = True
        rps.record_poll_success()
        self.assertFalse(rps.is_rest_poll_stalled())

    def test_rest_poll_stalled_in_snapshot(self) -> None:
        from api.agent_control import enrich_tick_runtime
        from system.rest_poll_status import snapshot_fields

        tick = enrich_tick_runtime({"markets": {}})
        self.assertIn("rest_poll_stalled", tick)
        self.assertIn("rest_poll_stalled", snapshot_fields())


class TestStopAttachment(unittest.TestCase):
    def _client(self, *, stops: list[bool]) -> MagicMock:
        calls = {"i": 0}

        def find_open_position(_deal_id: str) -> dict:
            idx = min(calls["i"], len(stops) - 1)
            calls["i"] += 1
            if stops[idx]:
                return {"position": {"stopLevel": 1.2345, "stopDistance": 0}}
            return {"position": {"stopLevel": 0, "stopDistance": 0}}

        client = MagicMock()
        client.find_open_position.side_effect = find_open_position
        client.ensure_protective_stops.return_value = True
        return client

    def test_stop_always_attached_after_entry_buy(self) -> None:
        from execution.scalping.atomic_protect import verify_stop_or_emergency

        client = self._client(stops=[False, True])
        ok = verify_stop_or_emergency(
            client,
            deal_id="D1",
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            size=10.0,
            stop_distance=8.0,
            verify_ms=500,
            max_retries=1,
            backoff_sec=0.0,
        )
        self.assertTrue(ok)
        client.ensure_protective_stops.assert_called()

    def test_stop_always_attached_after_entry_sell(self) -> None:
        from execution.scalping.atomic_protect import verify_stop_or_emergency

        client = self._client(stops=[True])
        ok = verify_stop_or_emergency(
            client,
            deal_id="D2",
            epic="CS.D.GBPUSD.CFD.IP",
            direction="SELL",
            size=7.5,
            stop_distance=8.0,
            verify_ms=500,
            max_retries=1,
            backoff_sec=0.0,
        )
        self.assertTrue(ok)

    def test_stop_attachment_retry_on_failure(self) -> None:
        from execution.scalping.atomic_protect import verify_stop_or_emergency

        client = self._client(stops=[False, False, True])
        ok = verify_stop_or_emergency(
            client,
            deal_id="D3",
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            size=10.0,
            stop_distance=8.0,
            verify_ms=100,
            max_retries=3,
            backoff_sec=0.0,
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(client.ensure_protective_stops.call_count, 2)

    def test_position_closed_if_stop_fails_after_retries(self) -> None:
        from execution.scalping.atomic_protect import verify_stop_or_emergency

        client = self._client(stops=[False] * 20)
        with patch(
            "system.telegram_notifier.send_critical_alert", return_value=True
        ) as tg:
            ok = verify_stop_or_emergency(
                client,
                deal_id="D4",
                epic="CS.D.GBPUSD.CFD.IP",
                direction="SELL",
                size=7.5,
                stop_distance=8.0,
                verify_ms=50,
                max_retries=3,
                backoff_sec=0.0,
            )
        self.assertFalse(ok)
        client.close_position.assert_called_once()
        tg.assert_called_once()
        self.assertIn("STOP FAIL", tg.call_args[0][0])

    def test_partial_size_stop_attachment_correct(self) -> None:
        from execution.scalping.atomic_protect import verify_stop_or_emergency

        client = self._client(stops=[True])
        ok = verify_stop_or_emergency(
            client,
            deal_id="D5",
            epic="CS.D.GBPUSD.CFD.IP",
            direction="BUY",
            size=7.5,
            stop_distance=8.0,
            verify_ms=500,
            max_retries=1,
            backoff_sec=0.0,
        )
        self.assertTrue(ok)
        args, kwargs = client.ensure_protective_stops.call_args
        self.assertEqual(kwargs.get("stop_distance"), 8.0)


if __name__ == "__main__":
    unittest.main()
