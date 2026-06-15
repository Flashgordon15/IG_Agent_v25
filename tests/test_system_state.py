"""Tests for BootState infrastructure (SystemState + BootCoordinator skeleton)."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from system.boot.gate2_runner import Gate2Runner
from system.boot_coordinator import BootCoordinator, boot_lifespan
from system.system_state import (
    BootPhase,
    GateStatus,
    SystemState,
    get_system_state,
    stamp_process_boot_start,
)


class SystemStateTests(unittest.TestCase):
    def setUp(self) -> None:
        SystemState.reset_singleton_for_tests()

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()

    def test_singleton_returns_same_instance(self) -> None:
        stamp_process_boot_start()
        a = get_system_state()
        b = SystemState.get()
        self.assertIs(a, b)

    def test_boot_stamp_anchors_started_at_before_gate_updates(self) -> None:
        from system.system_state import get_boot_started_at_wall, stamp_process_boot_start

        t0 = stamp_process_boot_start()
        state = get_system_state()
        snap = state.snapshot()
        self.assertAlmostEqual(float(snap["started_at_epoch"]), t0, places=3)
        self.assertIn("T", snap["started_at"])
        state.update_state(BootPhase.G1, 5, "Gate 1", gates_dict={"G1": {"status": "running"}})
        snap2 = state.snapshot()
        self.assertEqual(snap2["started_at"], snap["started_at"])
        self.assertEqual(snap2["started_at_epoch"], snap["started_at_epoch"])
        self.assertAlmostEqual(float(get_boot_started_at_wall() or 0), t0, places=3)

    def test_update_state_atomic_snapshot(self) -> None:
        state = get_system_state()
        state.update_state(
            BootPhase.G2,
            35,
            "Broker & State Hydrated",
            gates_dict={
                "G1": {"status": "complete", "detail": "API bound"},
                "G2": {"status": "running", "detail": "Authenticating…"},
            },
            streaming={"transport": "lightstreamer"},
        )
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "G2")
        self.assertEqual(snap["percent"], 35)
        self.assertEqual(snap["gates"]["G1"]["status"], "complete")
        self.assertEqual(snap["streaming"]["transport"], "lightstreamer")
        self.assertFalse(snap["ready"])

    def test_set_ready_flips_contract(self) -> None:
        state = get_system_state()
        state.set_ready()
        snap = state.snapshot()
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["phase"], "READY")
        self.assertEqual(snap["percent"], 100)

    def test_concurrent_reads_do_not_corrupt(self) -> None:
        state = get_system_state()
        errors: list[str] = []

        def writer() -> None:
            try:
                for i in range(100):
                    state.update_state(
                        BootPhase.G4,
                        i % 101,
                        f"step-{i}",
                        hydration={"ohlc_epics_ready": i % 6},
                    )
            except Exception as exc:
                errors.append(str(exc))

        def reader() -> None:
            try:
                for _ in range(100):
                    snap = state.snapshot()
                    assert 0 <= snap["percent"] <= 100
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errors, [])

    def test_mark_gate_failed_sets_error(self) -> None:
        state = get_system_state()
        state.mark_gate_failed("G2", error="401 unauthorized", detail="REST login failed")
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "FAILED")
        self.assertEqual(snap["error_gate"], "G2")
        self.assertEqual(snap["gates"]["G2"]["status"], "failed")


class BootCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        SystemState.reset_singleton_for_tests()

    def tearDown(self) -> None:
        SystemState.reset_singleton_for_tests()

    def test_skeleton_pipeline_reaches_ready(self) -> None:
        state = get_system_state()
        state.mark_gate_complete("G1")
        coordinator = BootCoordinator()
        coordinator.register_gate("G2", lambda: None)
        coordinator.register_gate("G3", lambda: None)
        coordinator.register_gate("G4", lambda: None)
        coordinator.register_gate("G5", lambda: get_system_state().set_ready())
        coordinator.run_pipeline()
        snap = coordinator.state.snapshot()
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["phase"], "READY")
        for gid in ("G1", "G2", "G3", "G4", "G5"):
            self.assertEqual(snap["gates"][gid]["status"], GateStatus.COMPLETE)

    @patch("system.boot.gate5_runner.Gate5Runner.run", lambda self: get_system_state().set_ready())
    @patch("system.boot.gate4_runner.Gate4Runner.run", lambda self: None)
    @patch("system.boot.gate3_runner.Gate3Runner.run", lambda self: None)
    @patch.object(Gate2Runner, "run", lambda self: None)
    @patch("system.boot.gate1_runner.Gate1Runner.run", lambda self: get_system_state().mark_gate_complete("G1"))
    def test_boot_lifespan_runs_pipeline_to_ready(self) -> None:
        class _App:
            state: object

        app = _App()
        app.state = type("State", (), {})()

        async def _run() -> None:
            async with boot_lifespan(app):
                for _ in range(50):
                    if get_system_state().snapshot()["ready"]:
                        return
                    await asyncio.sleep(0.02)
            self.fail("pipeline did not reach READY")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
