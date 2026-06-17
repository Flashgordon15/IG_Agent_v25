"""Gate 4 — OHLC hydration and dormant trading loop construction."""

from __future__ import annotations

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
        ready_count = 0

        def _on_loop_complete(loop: Any) -> None:
            nonlocal ready_count
            ready_count += 1
            pct = 65 + int(25 * ready_count / max(1, total))
            self._state.update_state(
                BootPhase.G4,
                min(pct, 89),
                f"Loading Market Data ({ready_count}/{total})",
                gates_dict=None,
                hydration={
                    "ohlc_epics_ready": ready_count,
                    "ohlc_epics_total": total,
                },
            )

        self._state.update_state(
            BootPhase.G4,
            68,
            "Loading Market Data",
            gates_dict=None,
            hydration={"ohlc_epics_ready": 0, "ohlc_epics_total": total},
        )

        from trading.ohlc_bootstrap import bootstrap_ohlc_parallel

        bootstrap_ohlc_parallel(rest, loops, on_loop_complete=_on_loop_complete)

        if cfg.get("intelligence_layer", {}).get("enabled"):
            try:
                from intelligence.microstructure import bootstrap_microstructure_parallel

                bootstrap_microstructure_parallel(rest, loops)
                log_engine("Gate4: microstructure historical warmup complete")
            except Exception as exc:
                log_engine(
                    f"Gate4: microstructure warmup skipped: {type(exc).__name__}: {exc}"
                )

        from api.agent_control import register_trading_loop

        register_trading_loop(orch)
        orch.start()

        self._state.update_state(
            BootPhase.G4,
            90,
            "Engines Armed (Standby)",
            gates_dict=None,
            hydration={
                "ohlc_epics_ready": total,
                "ohlc_epics_total": total,
            },
            loops={
                "built": total,
                "running": True,
                "accepting_ticks": False,
            },
        )
        log_engine(
            f"Gate4: {total} dormant loop thread(s) registered "
            f"(paused_at_boot=True, accepting_ticks=False)"
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
