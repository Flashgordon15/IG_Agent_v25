"""V6 post-READY handoff must re-unpause materialized TradingLoops."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def ready_system_state():
    from system.system_state import BootPhase, SystemState, get_system_state

    SystemState.reset_singleton_for_tests()
    state = get_system_state()
    for gid in ("G1", "G2", "G3", "G4", "G5"):
        state.mark_gate_complete(gid)
    state.update_state(BootPhase.G5, 100, "ACTIVE", ready=True)
    state.set_ready(label="ACTIVE")
    yield state
    SystemState.reset_singleton_for_tests()


def test_v6_handoff_unpauses_when_system_already_ready(ready_system_state) -> None:
    from runtime.market_orchestrator import V6InLoopCoroutineHandoff

    orch = MagicMock()
    orch._v6_skeleton_mode = True
    orch._v6_materialized = False
    orch._loops = [MagicMock(), MagicMock()]
    orch.is_running.return_value = False
    orch.config = MagicMock()

    handoff = V6InLoopCoroutineHandoff(orch, cfg=orch.config, rest_client=None)

    with (
        patch.object(handoff, "_materialize_full_loops_sync"),
        patch("runtime.market_orchestrator.attach_snapshot_handlers"),
        patch("runtime.market_orchestrator.AutonomicBootstrapStateMachine") as sm_cls,
        patch("api.agent_control.register_trading_loop"),
        patch("apex.microkernel.schedule_array_warmup"),
        patch.object(handoff, "_schedule_deferred_async_tail"),
        patch.dict("os.environ", {"IG_TEST_HARNESS": "1"}, clear=False),
    ):
        sm_cls.return_value.allocate_and_ready = MagicMock()
        handoff.run_full_handoff_sync()

    orch.unpause_from_boot.assert_called_once()
    orch._start_live_channels_impl.assert_called_once()
    assert orch._v6_materialized is True
    assert orch._v6_skeleton_mode is False


def test_v6_handoff_skips_unpause_when_not_ready() -> None:
    from runtime.market_orchestrator import V6InLoopCoroutineHandoff
    from system.system_state import SystemState

    SystemState.reset_singleton_for_tests()
    try:
        orch = MagicMock()
        orch._v6_skeleton_mode = True
        orch._v6_materialized = False
        orch._loops = [MagicMock()]
        orch.is_running.return_value = False
        orch.config = MagicMock()

        handoff = V6InLoopCoroutineHandoff(orch, cfg=orch.config, rest_client=None)

        with (
            patch.object(handoff, "_materialize_full_loops_sync"),
            patch("runtime.market_orchestrator.attach_snapshot_handlers"),
            patch("runtime.market_orchestrator.AutonomicBootstrapStateMachine") as sm_cls,
            patch("api.agent_control.register_trading_loop"),
            patch("apex.microkernel.schedule_array_warmup"),
            patch.object(handoff, "_schedule_deferred_async_tail"),
            patch.dict("os.environ", {"IG_TEST_HARNESS": "1"}, clear=False),
        ):
            sm_cls.return_value.allocate_and_ready = MagicMock()
            handoff.run_full_handoff_sync()

        orch.unpause_from_boot.assert_not_called()
    finally:
        SystemState.reset_singleton_for_tests()
