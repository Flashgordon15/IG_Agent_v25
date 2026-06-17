"""Flight Deck avionics — integration, queue, and non-blocking render tests."""

from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.emergency import reset_emergency_override_for_tests
from cockpit.launcher import (
    launch_flight_deck_after_gate4,
    reset_flight_deck_for_tests,
)
from cockpit.telemetry_bridge import (
    bridge_is_active,
    get_command_queue,
    get_telemetry_queue,
    reset_telemetry_bridge_for_tests,
    start_telemetry_bridge,
    stop_telemetry_bridge,
)
from intelligence.autopilot_scaling import intelligence_position_bonus
from intelligence.integration import apply_intelligence_pre_dispatch


class CockpitAvionicsTests(unittest.TestCase):
    def setUp(self) -> None:
        import cockpit.web_server as ws
        import system.protective_learning as pl

        ws._flight_deck_boot_seeded = False
        pl._autonomous_engine_boot_armed = False
        try:
            from intelligence.target_engine import reset_target_engine_for_tests

            reset_target_engine_for_tests()
        except Exception:
            pass

    def tearDown(self) -> None:
        import cockpit.web_server as ws
        import system.protective_learning as pl

        ws._flight_deck_boot_seeded = False
        pl._autonomous_engine_boot_armed = False
        stop_telemetry_bridge()
        reset_telemetry_bridge_for_tests()
        reset_flight_deck_for_tests()
        reset_emergency_override_for_tests()
        try:
            from intelligence.target_engine import reset_target_engine_for_tests

            reset_target_engine_for_tests()
        except Exception:
            pass
        try:
            from intelligence.intelligence_worker import reset_intelligence_worker_for_tests

            reset_intelligence_worker_for_tests()
        except Exception:
            pass

    def test_telemetry_queue_non_blocking_under_burst(self) -> None:
        start_telemetry_bridge(hz=20.0)
        tq = get_telemetry_queue()
        t0 = time.perf_counter()
        for i in range(2000):
            try:
                tq.put_nowait({"i": i})
            except queue.Full:
                try:
                    tq.get_nowait()
                except queue.Empty:
                    pass
                tq.put_nowait({"i": i})
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.5)

    def test_command_queue_drained_without_deadlock(self) -> None:
        """Bridge command worker must consume EMERGENCY_FLATTEN without blocking."""
        with patch(
            "cockpit.emergency.execute_emergency_cockpit_override",
            return_value={"status": "ok"},
        ) as mock_flatten:
            start_telemetry_bridge(hz=10.0)
            cmd_q = get_command_queue()
            cmd_q.put("EMERGENCY_FLATTEN")
            deadline = time.time() + 3.0
            while time.time() < deadline and not mock_flatten.called:
                time.sleep(0.05)
            self.assertTrue(mock_flatten.called, "command worker did not drain queue")

    @patch("cockpit.emergency.log_engine")
    @patch("api.agent_control.stop_trading")
    @patch("system.shutdown_cleanup.mark_manual_stop")
    def test_emergency_override_non_blocking(
        self,
        _mark: MagicMock,
        _stop: MagicMock,
        _log: MagicMock,
    ) -> None:
        from cockpit.emergency import execute_emergency_cockpit_override

        with patch(
            "system.credentials_loader.try_load_credentials",
            return_value=MagicMock(ok=False, credentials=None, error="test"),
        ):
            t0 = time.perf_counter()
            result = execute_emergency_cockpit_override()
            elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0)
        self.assertIn("status", result)

    def test_intelligence_pre_dispatch_blocks_turbulence(self) -> None:
        cfg = MagicMock()
        cfg.get = lambda k, d=None: {"enabled": True} if k == "intelligence_layer" else d
        signal = MagicMock(epic="IX.D.NASDAQ.IFM.IP")
        params = {"size": 1.0, "risk": 40.0}

        with patch(
            "intelligence.pipeline_bridge.get_intelligence_layer"
        ) as mock_layer:
            layer = MagicMock()
            layer.execution_adjustments.return_value = {
                "intelligence_spread_blocked": True,
                "intelligence_spread_z": 3.2,
                "intelligence_throttle_factor": 0.9,
            }
            mock_layer.return_value = layer
            merged, reject = apply_intelligence_pre_dispatch(
                signal, params, config=cfg
            )
        self.assertIsNotNone(reject)
        self.assertIn("INTELLIGENCE_SPREAD_BLOCK", reject or "")

    def test_autopilot_bonus_on_high_confidence_regime(self) -> None:
        cfg = {
            "intelligence_layer": {
                "enabled": True,
                "autopilot_scaling": {
                    "enabled": True,
                    "min_micro_confidence": 0.6,
                    "max_epic_bonus": 2,
                    "require_clear_spread": True,
                    "max_throttle_for_scale": 0.5,
                },
            }
        }

        class _Cfg:
            def get(self, key, default=None):
                return cfg.get(key, default)

        with patch(
            "intelligence.pipeline_bridge.get_intelligence_layer"
        ) as mock_layer:
            layer = MagicMock()
            spread = MagicMock(
                blocked=False,
                throttle_factor=0.0,
                z_score=0.5,
            )
            micro = MagicMock(
                confidence=0.85,
                regime="MOMENTUM_UP",
            )
            layer.spread_verdict.return_value = spread
            layer.microstructure_verdict.return_value = micro
            mock_layer.return_value = layer
            bonus, reason, rating = intelligence_position_bonus(_Cfg(), "IX.D.NASDAQ.IFM.IP")
        self.assertGreaterEqual(bonus, 1)
        self.assertGreater(rating, 0.0)
        self.assertIn("autopilot", reason)

    def test_launch_starts_web_cockpit_hub(self) -> None:
        """Gate 4 launcher must start bridge + web server (no tkinter)."""
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: {
            "intelligence_layer": {
                "cockpit": {
                    "enabled": True,
                    "auto_launch_after_gate4": True,
                    "telemetry_hz": 5.0,
                    "web_port": 18787,
                    "auto_open_browser": False,
                }
            }
        }.get(key, default)
        cfg.as_dict.return_value = {"instruments": {}}

        with patch("cockpit.launcher._resolve_epics", return_value=("IX.D.DOW.IFM.IP",)):
            with patch("cockpit.web_server.start_cockpit_web_server", return_value=True) as mock_web:
                launch_flight_deck_after_gate4(cfg)

        self.assertTrue(bridge_is_active())
        mock_web.assert_called_once()
        tq = get_telemetry_queue()
        self.assertIsNotNone(tq)

    def test_web_telemetry_drain_non_blocking(self) -> None:
        """Simulate UI poll loop reading queue — must stay sub-ms per tick."""
        tq: queue.Queue = queue.Queue()
        for i in range(100):
            tq.put({"gates": {"G1": {"status": "complete"}}, "spread": {}})
        t0 = time.perf_counter()
        for _ in range(100):
            latest = None
            try:
                while True:
                    latest = tq.get_nowait()
            except queue.Empty:
                break
            self.assertIsNotNone(latest)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.25)

    def test_web_cockpit_websocket_accepts(self) -> None:
        """Flight Deck /ws/telemetry must accept (not 403) after Gate 4."""
        from cockpit.web_server import _stop, broadcast_system_hot_reload, create_cockpit_app
        from starlette.testclient import TestClient

        _stop.clear()
        with TestClient(create_cockpit_app()) as client:
            with client.websocket_connect("/ws/telemetry") as ws:
                payload = ws.receive_json()
        self.assertIn("ts", payload)
        self.assertIn("gates", payload)
        controls = payload.get("cockpit_controls") or {}
        self.assertFalse(controls.get("manual_stop"))
        self.assertFalse(controls.get("disabled"))
        shadow = payload.get("shadow_trading") or {}
        self.assertEqual(str(shadow.get("mode", "")).upper(), "SHADOW")

    def test_system_hot_reload_frame(self) -> None:
        from cockpit.web_server import _stop, broadcast_system_hot_reload, create_cockpit_app
        from starlette.testclient import TestClient

        _stop.clear()
        broadcast_system_hot_reload(source="test")
        with TestClient(create_cockpit_app()) as client:
            with client.websocket_connect("/ws/telemetry") as ws:
                frame = ws.receive_json()
        self.assertEqual(frame.get("type"), "SYSTEM_HOT_RELOAD")

    def test_web_cockpit_logs_websocket(self) -> None:
        from cockpit.web_server import _stop, create_cockpit_app
        from starlette.testclient import TestClient

        _stop.clear()
        with TestClient(create_cockpit_app()) as client:
            with client.websocket_connect("/ws/logs") as ws:
                frame = ws.receive_json()
        self.assertEqual(frame.get("type"), "LOG_FRAME")
        self.assertIn("lines", frame)

    def test_web_cockpit_triage_websocket(self) -> None:
        from cockpit.web_server import _stop, create_cockpit_app
        from starlette.testclient import TestClient

        _stop.clear()
        with TestClient(create_cockpit_app()) as client:
            with client.websocket_connect("/ws/triage") as ws:
                frame = ws.receive_json()
        self.assertEqual(frame.get("type"), "TRIAGE_FRAME")
        self.assertIn("events", frame)


if __name__ == "__main__":
    unittest.main()
