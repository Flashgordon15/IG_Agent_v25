"""Tests for Gate 1 / Gate 2 boot runners."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import pytest

from system.boot.context import BootContext
from system.boot.exceptions import Gate1FatalError
from system.boot.gate1_runner import Gate1Runner
from system.boot.gate2_runner import Gate2Runner
from system.boot.gate3_runner import Gate3Runner
from system.boot.gate4_runner import Gate4Runner
from system.system_state import BootPhase, SystemState, get_system_state

# Ephemeral ports — never bind or evict production :8080 / :9199.
_HERMETIC_ENV = {
    "IG_AGENT_PYTEST": "1",
    "IG_API_PORT": "18080",
    "IG_SHADOW_PORT": "19199",
    "IG_PARALLEL_TRACK": "shadow",
    "IG_APEX_RUNTIME_MODE": "SANDBOX",
    "IG_APEX_PROTECT_PRODUCTION_PORTS": "1",
}


@pytest.fixture(autouse=True)
def _fast_boot_gate_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot gate tests are pure unit tests — skip 2s alert-dispatcher drain per case."""
    monkeypatch.setattr(
        "system.alert_dispatcher.reset_alert_dispatcher_for_tests",
        lambda: None,
    )


class _ImmediateThreadPoolExecutor:
    """Run submitted callables inline — no background IG hydration threads."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> _ImmediateThreadPoolExecutor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def submit(self, fn: object) -> MagicMock:
        future = MagicMock()
        future.result = lambda timeout=None: fn()  # type: ignore[misc]
        return future


class HermeticBootMixin:
    """Shared env isolation for all boot-gate test classes."""

    _env_patch: patch | None = None

    def _start_hermetic_env(self, *, harness: bool = False) -> None:
        env = dict(_HERMETIC_ENV)
        if harness:
            env["IG_TEST_HARNESS"] = "1"
        self._env_patch = patch.dict(os.environ, env, clear=False)
        self._env_patch.start()

    def _stop_hermetic_env(self) -> None:
        if self._env_patch is not None:
            self._env_patch.stop()
            self._env_patch = None


class Gate1RunnerTests(HermeticBootMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_hermetic_env()
        SystemState.reset_singleton_for_tests()

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()
        self._stop_hermetic_env()

    @patch("system.boot.port_eviction.reclaim_and_wait", return_value=True)
    @patch("system.demo_guard.validate_demo_only_startup", return_value=(True, "demo ok"))
    @patch("system.credentials_holder.bootstrap_credentials")
    @patch("system.instance_lock.acquire_instance_lock", return_value=(True, "ok"))
    @patch("system.config_validator.validate_config", return_value=(True, []))
    @patch("system.config_validator.emergency_stop_lock_present", return_value=False)
    @patch("system.boot.preflight_helpers.load_raw_config_dict", return_value={"epic": "TEST"})
    @patch("system.boot.preflight_helpers.rotate_oversized_logs")
    def test_gate1_success_marks_complete(
        self,
        _rotate: MagicMock,
        _raw: MagicMock,
        _emerg: MagicMock,
        _valid: MagicMock,
        _lock: MagicMock,
        _cred: MagicMock,
        _demo: MagicMock,
        _reclaim: MagicMock,
    ) -> None:
        ctx = BootContext()
        Gate1Runner(get_system_state(), ctx).run()
        snap = get_system_state().snapshot()
        self.assertTrue(get_system_state().gate_complete("G1"))
        self.assertEqual(snap["phase"], BootPhase.G1)
        self.assertEqual(snap["percent"], 10)
        self.assertIsNotNone(ctx.config)
        self.assertIsNotNone(ctx.raw_config)

    @patch("system.config_validator.emergency_stop_lock_present", return_value=True)
    @patch("system.boot.preflight_helpers.rotate_oversized_logs")
    def test_gate1_emergency_lock_raises(self, _rotate: MagicMock, _emerg: MagicMock) -> None:
        with self.assertRaises(Gate1FatalError):
            Gate1Runner().run()
        snap = get_system_state().snapshot()
        self.assertEqual(snap["error_gate"], "G1")
        self.assertEqual(snap["phase"], BootPhase.FAILED)


class Gate2RunnerTests(HermeticBootMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_hermetic_env()
        SystemState.reset_singleton_for_tests()
        get_system_state().mark_gate_complete("G1")

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()
        self._stop_hermetic_env()

    @patch("system.boot.gate2_runner.ThreadPoolExecutor", new=_ImmediateThreadPoolExecutor)
    @patch("system.guard.live_path_guard.is_live_production_track", return_value=False)
    @patch("feeder.mock_feed_engine.credentials_unconfigured", return_value=False)
    @patch("feeder.mock_feed_engine.should_use_mock_feed", return_value=False)
    @patch("runtime.ig_account_verify.verify_account_on_broker")
    @patch("system.ig_rest_session.ensure_shared_authenticated")
    @patch("system.startup_pipeline.check_account_type_demo")
    @patch("system.demo_guard.validate_demo_only_startup", return_value=(True, "demo"))
    @patch("system.boot.gate2_runner.get_credentials_holder")
    def test_gate2_hydration_success(
        self,
        holder_mock: MagicMock,
        _demo: MagicMock,
        acct_mock: MagicMock,
        auth_mock: MagicMock,
        verify_mock: MagicMock,
        _mock_feed: MagicMock,
        _creds_unconfigured: MagicMock,
        _live_prod: MagicMock,
    ) -> None:
        creds = MagicMock()
        creds.account_type = "DEMO"
        creds.masked_account_id.return_value = "****1234"
        holder_mock.return_value.credentials = creds

        acct_mock.return_value.ok = True

        rest = MagicMock()
        rest._base = "https://demo-api.ig.com/gateway/deal"
        rest.session.is_valid = True
        rest.open_positions.return_value = [
            {"position": {"size": 1.0}},
        ]
        rest.request.return_value.status_code = 200
        rest.request.return_value.json.return_value = {"workingOrders": [{"id": 1}]}
        rest.refresh_account_summary.return_value = {
            "balance": 10000.0,
            "available": 9500.0,
            "profit_loss": 0.0,
        }
        auth_mock.return_value = rest
        verify_mock.return_value = {"match": True, "accounts": []}

        ctx = BootContext()
        Gate2Runner(get_system_state(), ctx).run()
        snap = get_system_state().snapshot()
        self.assertNotEqual(snap.get("error_gate"), "G2")
        self.assertTrue(snap["hydration"]["positions_synced"])
        self.assertTrue(snap["hydration"]["orders_synced"])
        self.assertIs(ctx.rest_client, rest)
        self.assertIsNotNone(ctx.config)
        rest.open_positions.assert_called_once()
        rest.refresh_account_summary.assert_called_once()

    @patch("system.boot.gate2_runner.get_credentials_holder")
    def test_gate2_missing_credentials_uses_mock_feed(self, holder_mock: MagicMock) -> None:
        holder_mock.return_value.credentials = None
        ctx = BootContext()
        Gate2Runner(get_system_state(), ctx).run()
        snap = get_system_state().snapshot()
        self.assertNotEqual(snap.get("error_gate"), "G2")
        self.assertTrue(snap["hydration"]["positions_synced"])
        self.assertIsNotNone(ctx.rest_client)
        self.assertTrue(ctx.account_verify.get("mock_feed"))


class Gate3RunnerTests(HermeticBootMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_hermetic_env()
        SystemState.reset_singleton_for_tests()
        get_system_state().mark_gate_complete("G1")
        get_system_state().mark_gate_complete("G2")

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()
        self._stop_hermetic_env()

    @patch("system.boot.gate3_runner.time.sleep")
    @patch("system.boot.gate3_runner._open_market_epics", side_effect=lambda epics: list(epics))
    @patch("feeder.pricing_transport.reference_transport_is_yahoo", return_value=False)
    @patch("system.boot.gate3_runner._first_live_tick_epic", return_value="CS.D.EURUSD.CFD.IP")
    @patch("system.boot.gate3_runner._stream_heartbeat_ok", return_value=True)
    @patch("runtime.agent_bootstrap.start_market_stream")
    @patch("execution.position_protect_hub.wire_hub_quotes_to_position_protect")
    @patch("api.snapshot_store.wire_hub_quotes_to_dashboard")
    @patch("ig_api.streaming_factory.resolve_streaming_transport", return_value=("rest_poll", "config"))
    def test_gate3_stream_confirmed(
        self,
        _transport: MagicMock,
        _dash: MagicMock,
        _protect: MagicMock,
        stream_mock: MagicMock,
        _hb: MagicMock,
        _tick: MagicMock,
        _yahoo_flag: MagicMock,
        _open_epics: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        client = MagicMock()
        client.transport_label = "rest_poll"
        stream_mock.return_value = client

        cfg = MagicMock()
        cfg.as_dict.return_value = {"instruments": {}}
        cfg.epic = "CS.D.EURUSD.CFD.IP"
        cfg.streaming_transport = "rest_poll"
        cfg.refresh_seconds = 5.0

        ctx = BootContext()
        ctx.config = cfg
        ctx.rest_client = MagicMock()

        with patch("trading.instrument_registry.InstrumentRegistry") as reg_mock:
            reg_mock.return_value.get_enabled_with_ids.return_value = [
                ("eurusd", {"epic": "CS.D.EURUSD.CFD.IP"}),
            ]
            Gate3Runner(get_system_state(), ctx, timeout_sec=1.0).run()

        snap = get_system_state().snapshot()
        self.assertNotEqual(snap.get("error_gate"), "G3")
        self.assertTrue(snap["streaming"]["heartbeat_ok"])
        self.assertIs(ctx.stream_client, client)
        _sleep.assert_not_called()

    @patch("system.boot.gate3_runner.time.sleep")
    @patch("system.boot.gate3_runner._open_market_epics", side_effect=lambda epics: list(epics))
    @patch("feeder.yahoo_quote_poller.start_yahoo_quote_poller")
    @patch("feeder.pricing_transport.reference_transport_is_yahoo", return_value=True)
    @patch("system.boot.gate3_runner._first_live_tick_epic", return_value="CS.D.CFPGOLD.CFP.IP")
    @patch("execution.position_protect_hub.wire_hub_quotes_to_position_protect")
    @patch("api.snapshot_store.wire_hub_quotes_to_dashboard")
    def test_gate3_yahoo_reference_confirmed(
        self,
        _dash: MagicMock,
        _protect: MagicMock,
        _tick: MagicMock,
        _yahoo_flag: MagicMock,
        poller_mock: MagicMock,
        _open_epics: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        poller = MagicMock()
        poller.running = True
        poller.stats.return_value = {"polls": 1, "published": 1, "errors": 0}
        poller_mock.return_value = poller

        cfg = MagicMock()
        cfg.as_dict.return_value = {"instruments": {}}
        cfg.epic = "CS.D.CFPGOLD.CFP.IP"
        cfg.get.side_effect = lambda key, default=None: (
            {"enabled": False} if key == "intelligence_layer" else default
        )

        ctx = BootContext()
        ctx.config = cfg
        ctx.rest_client = MagicMock()

        with patch("trading.instrument_registry.InstrumentRegistry") as reg_mock:
            reg_mock.return_value.get_enabled_with_ids.return_value = [
                ("gold", {"epic": "CS.D.CFPGOLD.CFP.IP"}),
            ]
            Gate3Runner(get_system_state(), ctx, timeout_sec=1.0).run()

        snap = get_system_state().snapshot()
        self.assertNotEqual(snap.get("error_gate"), "G3")
        self.assertEqual(snap["streaming"]["transport"], "yahoo")
        self.assertTrue(snap["streaming"]["heartbeat_ok"])
        self.assertIsNone(ctx.stream_client)
        _sleep.assert_not_called()

    def test_gate3_missing_rest_client_fails(self) -> None:
        ctx = BootContext()
        ctx.config = MagicMock()
        Gate3Runner(get_system_state(), ctx, timeout_sec=0.5).run()
        snap = get_system_state().snapshot()
        self.assertEqual(snap["error_gate"], "G3")
        self.assertIn("Streaming initialization failed", snap["error"] or "")


class Gate4RunnerTests(HermeticBootMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_hermetic_env(harness=True)
        SystemState.reset_singleton_for_tests()
        for gid in ("G1", "G2", "G3"):
            get_system_state().mark_gate_complete(gid)

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()
        self._stop_hermetic_env()

    @patch("cockpit.port_cleanup.clear_port_8080")
    @patch("cockpit.launcher.launch_flight_deck_after_gate4")
    @patch("intelligence.target_engine.initialize_target_engine")
    @patch("apex.microkernel.schedule_array_warmup")
    @patch("api.agent_control.register_trading_loop")
    @patch("runtime.agent_bootstrap.build_market_orchestrator")
    def test_gate4_dormant_loops_registered(
        self,
        orch_mock: MagicMock,
        _register: MagicMock,
        _warmup: MagicMock,
        _target_engine: MagicMock,
        _flight_deck: MagicMock,
        _port_clear: MagicMock,
    ) -> None:
        loop = MagicMock()
        orch = MagicMock()
        orch.loops = [loop, loop]
        orch_mock.return_value = orch

        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: (
            {"enabled": False, "cockpit": {"enabled": False}}
            if key == "intelligence_layer"
            else default
        )

        ctx = BootContext()
        ctx.config = cfg
        ctx.rest_client = MagicMock()

        Gate4Runner(get_system_state(), ctx).run()

        orch_mock.assert_called_once()
        call_kw = orch_mock.call_args.kwargs
        self.assertTrue(call_kw["boot_mode"])
        self.assertTrue(call_kw["paused_at_boot"])
        self.assertTrue(call_kw["defer_ohlc"])
        orch.start.assert_called_once()
        _register.assert_called_once_with(orch)
        _warmup.assert_not_called()
        _port_clear.assert_not_called()
        _flight_deck.assert_not_called()

        snap = get_system_state().snapshot()
        self.assertNotEqual(snap.get("error_gate"), "G4")
        self.assertEqual(snap["hydration"]["ohlc_epics_total"], 2)
        self.assertEqual(snap["loops"]["built"], 2)
        self.assertTrue(snap["loops"]["running"])
        self.assertFalse(snap["loops"]["accepting_ticks"])
        self.assertIs(ctx.orchestrator, orch)


class Gate5RunnerTests(HermeticBootMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_hermetic_env()
        SystemState.reset_singleton_for_tests()
        for gid in ("G1", "G2", "G3", "G4"):
            get_system_state().mark_gate_complete(gid)

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()
        self._stop_hermetic_env()

    @patch("system.boot.gate5_runner._spawn_background_deploy_verify")
    @patch("system.boot.post_ready_services.start_post_ready_services")
    def test_gate5_sets_ready_and_unpauses(
        self,
        post_ready_mock: MagicMock,
        _bg_verify: MagicMock,
    ) -> None:
        orch = MagicMock()
        ctx = BootContext()
        ctx.orchestrator = orch
        get_system_state().update_state(
            BootPhase.G4,
            90,
            "Engines Armed (Standby)",
            loops={"built": 2, "running": True, "accepting_ticks": False},
        )

        from system.boot.gate5_runner import Gate5Runner

        Gate5Runner(get_system_state(), ctx).run()

        orch.unpause_from_boot.assert_called_once()
        post_ready_mock.assert_called_once_with(ctx)
        snap = get_system_state().snapshot()
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["phase"], "READY")
        self.assertEqual(snap["percent"], 100)
        self.assertEqual(snap["phase_label"], "ACTIVE")
        self.assertTrue(snap["loops"]["accepting_ticks"])

    def test_gate5_fails_without_orchestrator(self) -> None:
        from system.boot.gate5_runner import Gate5Runner

        Gate5Runner().run()
        snap = get_system_state().snapshot()
        self.assertEqual(snap["error_gate"], "G5")


if __name__ == "__main__":
    unittest.main()
