"""Gate 1 pre-uvicorn entry — imports only Gate1Runner (no G2–G5 / ig_api chain)."""

from __future__ import annotations

import os

from system.boot.context import BootContext
from system.boot.gate1_runner import Gate1Runner
from system.system_state import get_system_state, stamp_process_boot_start


def run_gate1_preflight(context: BootContext | None = None) -> BootContext:
    """
    Run Gate 1 synchronously before ``uvicorn.run``.

    Raises ``Gate1FatalError`` on failure after updating ``SystemState``.
    """
    stamp_process_boot_start()

    if os.environ.get("DAY1_GENESIS", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from system.day1_genesis_reset import run_day1_genesis_reset

            run_day1_genesis_reset()
        except Exception as exc:
            from system.engine_log import log_engine

            log_engine(
                f"Day1 genesis reset failed (continuing boot): {type(exc).__name__}: {exc}"
            )

    ctx = context or BootContext()
    Gate1Runner(get_system_state(), ctx).run()
    return ctx
