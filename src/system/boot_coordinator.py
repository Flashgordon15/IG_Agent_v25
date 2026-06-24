"""
BootState pipeline coordinator — sequential gatekeeper execution.

Runs the G1→G5 boot pipeline off the asyncio event loop via ``asyncio.to_thread``
so Uvicorn can bind and serve ``SystemState`` snapshots while gates progress.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from system.boot.context import BootContext
from system.boot.exceptions import Gate1FatalError
from system.system_state import (
    GATE_IDS,
    BootPhase,
    GateId,
    GateStatus,
    SystemState,
    get_system_state,
)

logger = logging.getLogger(__name__)

GateRunner = Callable[[], None]


class BootCoordinator:
    """
    Owns sequential gate execution. Production gates G1–G5 register via
    ``create_boot_coordinator``.
    """

    def __init__(
        self,
        state: SystemState | None = None,
        *,
        context: BootContext | None = None,
        register_skeleton_gates: bool = True,
    ) -> None:
        self._state = state or get_system_state()
        self._context = context or BootContext()
        self._gate_runners: dict[GateId, GateRunner] = {}
        if register_skeleton_gates:
            self._register_skeleton_runners()

    @property
    def state(self) -> SystemState:
        return self._state

    @property
    def context(self) -> BootContext:
        return self._context

    def register_gate(self, gate_id: GateId, runner: GateRunner) -> None:
        """Replace the runner for a gate (production Gate1Runner / Gate2Runner)."""
        self._gate_runners[gate_id] = runner

    def ensure_g1_complete(self) -> None:
        """Run Gate 1 if not already complete (fallback when main skipped preflight)."""
        if not self._state.gate_complete("G1"):
            self._run_gate("G1")

    def run_pipeline(self) -> None:
        """
        Execute gates G2→G5 sequentially on a worker thread.

        G1 must complete before the API is considered ready for Gate 2 onward.
        """
        from system.boot.non_blocking_bootstrap import (
            non_blocking_boot_enabled,
            run_gate_with_exponential_backoff,
        )

        logger.info("BootCoordinator: pipeline started")
        try:
            if not self._state.gate_complete("G1"):
                logger.warning("BootCoordinator: G1 incomplete — running Gate 1")
                self._run_gate("G1")
                if self._pipeline_failed():
                    return

            for gate_id in GATE_IDS[1:]:
                previous = GATE_IDS[GATE_IDS.index(gate_id) - 1]
                if not self._state.gate_complete(previous):
                    logger.error(
                        "BootCoordinator: gatekeeper blocked %s — %s incomplete",
                        gate_id,
                        previous,
                    )
                    self._state.mark_gate_failed(
                        gate_id,
                        error=f"Gatekeeper: {previous} not complete",
                    )
                    return
                if non_blocking_boot_enabled() and gate_id in ("G2", "G3"):
                    runner = self._gate_runners.get(gate_id)
                    if runner is None:
                        raise RuntimeError(f"No runner registered for {gate_id}")
                    ok = run_gate_with_exponential_backoff(
                        gate_id,
                        lambda g=gate_id: self._run_gate(g),
                    )
                    if not ok:
                        self._state.mark_gate_failed(
                            gate_id,
                            error=f"{gate_id} hydration exhausted retries",
                        )
                        return
                else:
                    self._run_gate(gate_id)
                if self._pipeline_failed():
                    logger.info(
                        "BootCoordinator: pipeline halted at %s (%s)",
                        self._state.snapshot().get("error_gate"),
                        self._state.snapshot().get("error"),
                    )
                    return

            if all(self._state.gate_complete(gid) for gid in GATE_IDS):
                if self._state.snapshot_model().ready:
                    logger.info("BootCoordinator: SystemState.READY")
                else:
                    logger.warning(
                        "BootCoordinator: all gates complete but ready=False"
                    )
        except Gate1FatalError:
            raise
        except Exception as exc:
            logger.exception("BootCoordinator: pipeline failed")
            active_gate = self._first_incomplete_gate()
            self._state.mark_gate_failed(
                active_gate,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _pipeline_failed(self) -> bool:
        snap = self._state.snapshot_model()
        return snap.phase == BootPhase.FAILED or snap.error_gate is not None

    def _first_incomplete_gate(self) -> GateId:
        for gate_id in GATE_IDS:
            if not self._state.gate_complete(gate_id):
                return gate_id
        return "G5"

    def _run_gate(self, gate_id: GateId) -> None:
        runner = self._gate_runners.get(gate_id)
        if runner is None:
            raise RuntimeError(f"No runner registered for {gate_id}")
        self._state.mark_gate_running(gate_id)
        runner()
        if self._state.snapshot_model().gates[gate_id].status != GateStatus.FAILED:
            if not self._state.gate_complete(gate_id):
                self._state.mark_gate_complete(gate_id)

    def _register_skeleton_runners(self) -> None:
        """Test-only placeholders when gates are registered manually."""
        pass


@asynccontextmanager
async def boot_lifespan(app: Any) -> AsyncIterator[dict[str, Any]]:
    """
    FastAPI lifespan hook — bind boot pipeline to the Uvicorn lifecycle.

    Gate 1 should run via ``run_gate1_preflight()`` before ``uvicorn.run``.
    Heavy API routers mount here (after bind) before G2→G5 on a worker thread.
    """
    import asyncio

    from api.server_deferred import mount_deferred_routers

    loop = asyncio.get_running_loop()
    from system.boot.boot_loop_holder import set_boot_loop

    set_boot_loop(loop)
    await asyncio.to_thread(mount_deferred_routers, app, loop)
    if hasattr(app.state, "_mount_done"):
        app.state._mount_done.set()

    from system.boot.coordinator_factory import create_boot_coordinator

    preflight_ctx = getattr(app.state, "boot_context", None)
    coordinator = create_boot_coordinator(context=preflight_ctx)

    if not coordinator.state.gate_complete("G1"):
        try:
            coordinator.ensure_g1_complete()
        except Gate1FatalError:
            logger.exception("BootCoordinator: Gate 1 failed during lifespan")

    from system.boot.non_blocking_bootstrap import (
        non_blocking_boot_enabled,
        start_background_gate_hydration,
    )

    if non_blocking_boot_enabled():

        async def _await_hydration_worker() -> None:
            worker = start_background_gate_hydration(coordinator)
            thread = worker._thread
            while thread is not None and thread.is_alive():
                await asyncio.sleep(0.25)

        pipeline_task = asyncio.create_task(
            _await_hydration_worker(),
            name="boot-coordinator-pipeline",
        )
    else:
        pipeline_task = asyncio.create_task(
            asyncio.to_thread(coordinator.run_pipeline),
            name="boot-coordinator-pipeline",
        )

    app.state.boot_coordinator = coordinator
    app.state.system_state = coordinator.state
    app.state.boot_context = coordinator.context

    try:
        yield {
            "boot_coordinator": coordinator,
            "system_state": coordinator.state,
            "boot_context": coordinator.context,
        }
    finally:
        if not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except asyncio.CancelledError:
                logger.info("BootCoordinator: pipeline cancelled during shutdown")
