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
        import os

        harness_mode = os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        cfg = self._context.config
        rest = self._context.rest_client
        if cfg is None or rest is None:
            raise RuntimeError("Gate 4 requires config and rest_client from prior gates")

        try:
            from system.recovery_mgr import run_v62_pre_loop_disaster_recovery

            run_v62_pre_loop_disaster_recovery(
                rest_client=rest,
                config=cfg,
                system_state=self._state,
                boot_context=self._context,
            )
            log_engine("Gate4: V6.2 disaster recovery handshake complete (pre-loop)")
        except Exception as exc:
            log_engine(
                f"Gate4: V6.2 disaster recovery skipped: {type(exc).__name__}: {exc}"
            )

        from runtime.agent_bootstrap import build_market_orchestrator

        try:
            from runtime.market_orchestrator import preflight_v6_instant_bootstrap

            preflight_v6_instant_bootstrap(config=cfg)
            log_engine("Gate4: V6 instant preflight memory bound")
        except Exception as exc:
            log_engine(
                f"Gate4: V6 instant preflight skipped: {type(exc).__name__}: {exc}"
            )

        try:
            from intelligence.telemetry_daemon import start_v2_telemetry_daemon

            start_v2_telemetry_daemon(config=cfg)
            from intelligence.telemetry_daemon import maybe_arm_ui_stress_render_from_env

            maybe_arm_ui_stress_render_from_env(delay_sec=10.0)
            log_engine("Gate4: V4MicroReactor armed pre-orchestrator (memory-bound ingress)")
        except Exception as exc:
            log_engine(
                f"Gate4: V4MicroReactor early arm skipped: {type(exc).__name__}: {exc}"
            )

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
        if not harness_mode:
            from apex.microkernel import schedule_array_warmup

            # V6: array warmup runs inside coroutine handoff after loop materialization.
            if not getattr(orch, "_v6_skeleton_mode", False):
                schedule_array_warmup(rest, loops, cfg)
        else:
            log_engine("Gate4: harness fast-path — skipping microkernel array warmup")

        if harness_mode:
            try:
                orch.start()
                log_engine(
                    f"Gate4: harness sync start — {total} loop thread(s) online"
                )
            except Exception as exc:
                log_engine(
                    f"Gate4: harness orchestrator start error: {type(exc).__name__}: {exc}"
                )
        else:
            log_engine(
                f"Gate4: V6 skeleton registered — {total} loop(s); "
                "materialization deferred to Gate5 blocking handoff"
            )

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

        if not harness_mode and cfg.get("intelligence_layer", {}).get("enabled"):
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
        if (
            not harness_mode
            and isinstance(cockpit_cfg, dict)
            and cockpit_cfg.get("enabled")
        ):
            try:
                from system.node_profile import get_node_profile, is_shadow_node

                profile = get_node_profile()
                if is_shadow_node() or profile.is_testbed:
                    log_engine(
                        f"Gate4: {profile.kind} profile — production :8080 protected; "
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
