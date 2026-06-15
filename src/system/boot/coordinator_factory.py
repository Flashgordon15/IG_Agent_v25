"""BootCoordinator construction and Gate 1 pre-uvicorn entry."""

from __future__ import annotations

from system.boot.context import BootContext
from system.boot.gate1_runner import Gate1Runner
from system.boot_coordinator import BootCoordinator
from system.system_state import get_system_state


def create_boot_coordinator(
    context: BootContext | None = None,
    *,
    register_skeleton_gates: bool = True,
) -> BootCoordinator:
    """
    Build a coordinator with production Gate 1–5 runners.

    Pass a shared ``BootContext`` so gates share config, REST client, stream, and orchestrator.
    Gates G2–G5 are imported lazily so Gate 1 preflight stays lightweight.
    """
    from system.boot.gate2_runner import Gate2Runner
    from system.boot.gate3_runner import Gate3Runner
    from system.boot.gate4_runner import Gate4Runner
    from system.boot.gate5_runner import Gate5Runner

    ctx = context or BootContext()
    coordinator = BootCoordinator(context=ctx, register_skeleton_gates=False)
    state = coordinator.state
    coordinator.register_gate("G1", Gate1Runner(state, ctx).run)
    coordinator.register_gate("G2", Gate2Runner(state, ctx).run)
    coordinator.register_gate("G3", Gate3Runner(state, ctx).run)
    coordinator.register_gate("G4", Gate4Runner(state, ctx).run)
    coordinator.register_gate("G5", Gate5Runner(state, ctx).run)
    return coordinator


def run_gate1_preflight(context: BootContext | None = None) -> BootContext:
    """Backward-compatible alias — prefer ``system.boot.gate1_preflight`` from main."""
    from system.boot.gate1_preflight import run_gate1_preflight as _run

    return _run(context)
