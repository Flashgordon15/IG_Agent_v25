"""Gate 4 — OHLC hydration and dormant trading loop construction."""

from __future__ import annotations

import threading
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine
from system.system_state import BootPhase, SystemState, get_system_state


class Gate4Runner:
    """Bootstrap OHLC, build orchestrator, start dormant loop threads."""

    def __init__(
        self,
        state: SystemState | None = None,
        context: BootContext | None = None,
    ) -> None:
        self._state = state or get_system_state()
        self._context = context or BootContext()

    def run(self) -> None:
        self._state.update_state(
            BootPhase.G4,
            65,
            "Loading Market Data",
            gates_dict=None,
        )

        try:
            self._execute()
        except Exception as exc:
            message = f"Gate 4 hydration failed: {type(exc).__name__}: {exc}"
            log_engine(f"Gate4 FATAL: {message}")
            self._state.mark_gate_failed(
                "G4",
                error=message,
                detail="OHLC bootstrap or loop construction failed",
            )

    def _execute(self) -> None:
        cfg = self._context.config
        rest = self._context.rest_client
        if cfg is None or rest is None:
            raise RuntimeError("Gate 4 requires config and rest_client from prior gates")

        from runtime.agent_bootstrap import build_market_orchestrator

        orch = build_market_orchestrator(
            cfg,
            rest_client=rest,
            boot_mode=True,
            paused_at_boot=True,
            defer_ohlc=True,
        )
        loops = list(orch.loops)
        total = len(loops)
        if total == 0:
            raise RuntimeError("No trading loops built for enabled instruments")

        self._context.orchestrator = orch

        self._state.update_state(
            BootPhase.WARMING,
            0,
            "Compiling Vector Arrays",
            gates_dict=None,
            hydration={"ohlc_epics_ready": 0, "ohlc_epics_total": total},
            loops={
                "built": total,
                "running": True,
                "accepting_ticks": False,
            },
            ready=False,
        )

        from api.agent_control import register_trading_loop

        register_trading_loop(orch)

        # Parallel array warmup must start before any blocking orchestrator work —
        # orch.start() can stall on REST/market-status refresh and previously
        # prevented schedule_array_warmup from ever running (0/256 splash hang).
        from apex.microkernel import schedule_array_warmup

        schedule_array_warmup(rest, loops, cfg)

        def _start_orchestrator() -> None:
            try:
                orch.start()
                log_engine(
                    f"Gate4: {total} dormant loop thread(s) online "
                    "(paused_at_boot=True)"
                )
            except Exception as exc:
                log_engine(
                    f"Gate4: orchestrator start error: {type(exc).__name__}: {exc}"
                )

        threading.Thread(
            target=_start_orchestrator,
            name="gate4-orchestrator-start",
            daemon=True,
        ).start()

        self._state.update_state(
            BootPhase.G4,
            72,
            "Engines Armed (Array Warmup)",
            gates_dict=None,
            hydration={"ohlc_epics_ready": 0, "ohlc_epics_total": total},
            loops={
                "built": total,
                "running": True,
                "accepting_ticks": False,
            },
        )
        log_engine(
            f"Gate4: {total} dormant loop thread(s) registered — "
            "detached array warmup started (paused_at_boot=True)"
        )

        if cfg.get("intelligence_layer", {}).get("enabled"):
            try:
                from intelligence.target_engine import initialize_target_engine

                store = None
                if loops:
                    store = getattr(loops[0], "_store", None)
                initialize_target_engine(cfg, rest, store=store)
                log_engine("Gate4: Target-Seeking Alpha Engine initialized")
            except Exception as exc:
                log_engine(
                    f"Gate4: target engine init skipped: {type(exc).__name__}: {exc}"
                )

        cockpit_cfg = cfg.get("intelligence_layer", {}).get("cockpit", {})
        if isinstance(cockpit_cfg, dict) and cockpit_cfg.get("enabled"):
            try:
                from system.node_profile import get_node_profile, is_shadow_node

                profile = get_node_profile()
                if is_shadow_node():
                    log_engine(
                        "Gate4: shadow profile — production :8080/:8787 protected; "
                        f"cockpit binds :{profile.cockpit_port} only"
                    )
                else:
                    from cockpit.port_cleanup import clear_port_8080

                    cleared = clear_port_8080()
                    if cleared:
                        log_engine(f"Gate4: cleared {len(cleared)} process(es) on :8080")
            except Exception as exc:
                log_engine(f"Gate4: port cleanup skipped: {type(exc).__name__}: {exc}")

            try:
                from cockpit.launcher import launch_flight_deck_after_gate4

                launch_flight_deck_after_gate4(cfg)
            except Exception as exc:
                log_engine(
                    f"Gate4: flight deck launch skipped: {type(exc).__name__}: {exc}"
                )
