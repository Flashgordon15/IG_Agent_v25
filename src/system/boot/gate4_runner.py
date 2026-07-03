"""Gate 4 — OHLC hydration and dormant trading loop construction."""

from __future__ import annotations

import threading
import time
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine
from system.system_state import BootPhase, GateStatus, SystemState, get_system_state

_G4_HEAL_MIN_ELAPSED_SEC = 15.0
_G4_HEAL_LOCK_TIMEOUT_SEC = 2.0
_G4_DR_TIMEOUT_SEC = 5.0
_G4_BUILD_TIMEOUT_SEC = 60.0
_gate4_started_mono: float | None = None
_gate4_build_lock = threading.Lock()


def note_g4_started() -> None:
    global _gate4_started_mono
    _gate4_started_mono = time.monotonic()


def _gate4_loops_built() -> int:
    state = get_system_state()
    snap = state.try_snapshot(timeout=0.25)
    if snap is None:
        snap = {}
    built = int((snap.get("loops") or {}).get("built") or 0)
    if built > 0:
        return built
    try:
        from api.agent_control import get_trading_loop

        orch = get_trading_loop()
        loops = list(getattr(orch, "loops", []) or [])
        return len(loops)
    except Exception:
        return 0


def _g4_should_exit(state: SystemState) -> bool:
    from system.boot.gate_sideband import gate_is_done

    return gate_is_done(state, "G4")


def _build_orchestrator_off_thread(
    cfg: Any,
    rest: Any,
    *,
    timeout_sec: float = _G4_BUILD_TIMEOUT_SEC,
) -> Any:
    """Run build_market_orchestrator off the hydration thread (avoids SystemState lock wedge)."""
    result: dict[str, Any] = {"orch": None, "err": None}

    def _build() -> None:
        try:
            from runtime.agent_bootstrap import build_market_orchestrator

            result["orch"] = build_market_orchestrator(
                cfg,
                rest_client=rest,
                boot_mode=True,
                paused_at_boot=True,
                defer_ohlc=True,
            )
        except Exception as exc:
            result["err"] = exc

    worker = threading.Thread(target=_build, name="gate4-orchestrator-build", daemon=True)
    worker.start()
    worker.join(timeout=timeout_sec)
    if worker.is_alive():
        raise RuntimeError(
            f"build_market_orchestrator timed out after {timeout_sec:.0f}s"
        )
    if result["err"] is not None:
        raise result["err"]
    orch = result["orch"]
    if orch is None:
        raise RuntimeError("build_market_orchestrator returned None")
    return orch


def _finalize_g4_complete(
    *,
    state: SystemState,
    context: BootContext,
    orch: Any,
    loops: list[Any],
    total: int,
    cfg: Any,
    rest: Any,
    harness_mode: bool,
    detail: str,
) -> None:
    """Register orchestrator, update state, and mark G4 complete (sideband + lock)."""
    context.orchestrator = orch

    from api.agent_control import register_trading_loop

    register_trading_loop(orch)

    from system.boot.gate_sideband import mark_gate_sideband

    mark_gate_sideband("G4")
    if state._lock.acquire(timeout=3.0):
        try:
            state.update_state(
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
            if state._snapshot.gates["G4"].status != GateStatus.COMPLETE:
                state.mark_gate_complete("G4", detail=detail)
        finally:
            state._lock.release()
    else:
        log_engine(f"Gate4: sideband complete — lock busy detail={detail}")

    log_engine(
        f"Gate4: {total} dormant loop thread(s) registered — "
        f"detached array warmup started (paused_at_boot=True) [{detail}]"
    )

    if not harness_mode:
        from apex.microkernel import schedule_array_warmup

        if not getattr(orch, "_v6_skeleton_mode", False):
            schedule_array_warmup(rest, loops, cfg)
    else:
        log_engine("Gate4: harness fast-path — skipping microkernel array warmup")

    _schedule_post_gate4_optional(
        cfg=cfg, rest=rest, loops=loops, harness_mode=harness_mode
    )


def force_gate4_orchestrator_build(
    *,
    state: SystemState,
    context: BootContext,
) -> bool:
    """Build orchestrator off the wedged hydration thread (watchdog recovery path)."""
    if _g4_should_exit(state) or _gate4_loops_built() > 0:
        return False
    resolved = _resolve_g4_boot_context(context)
    if resolved is None:
        log_engine("Gate4: force build skipped — config/rest unavailable")
        return False
    cfg, rest = resolved
    if not _gate4_build_lock.acquire(timeout=15.0):
        log_engine("Gate4: force build deferred — primary runner holds build lock")
        return False
    try:
        if _g4_should_exit(state) or _gate4_loops_built() > 0:
            return False
        import os

        harness_mode = os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        try:
            orch = _build_orchestrator_off_thread(cfg, rest, timeout_sec=_G4_BUILD_TIMEOUT_SEC)
        except Exception as exc:
            log_engine(f"Gate4: force build failed: {type(exc).__name__}: {exc}")
            return False
        loops = list(getattr(orch, "loops", []) or [])
        total = len(loops)
        if total <= 0:
            return False
        _finalize_g4_complete(
            state=state,
            context=context,
            orch=orch,
            loops=loops,
            total=total,
            cfg=cfg,
            rest=rest,
            harness_mode=harness_mode,
            detail="watchdog_force_build",
        )
        log_engine(f"Gate4: watchdog force-build — {total} loop(s) registered")
        return True
    finally:
        _gate4_build_lock.release()


def _resolve_g4_boot_context(context: BootContext) -> tuple[Any, Any] | None:
    """Ensure config + rest_client are populated for G4 build paths."""
    cfg = context.config
    rest = context.rest_client
    if cfg is None and context.raw_config:
        from system.config import Config
        from system.config_validator import apply_config_defaults

        merged = apply_config_defaults(dict(context.raw_config))
        cfg = Config(_data=merged)
        context.config = cfg
    if rest is None:
        try:
            from system.credentials_holder import get_credentials_holder
            from system.ig_rest_session import get_shared_rest_client

            holder = get_credentials_holder()
            if holder.credentials is not None:
                rest = get_shared_rest_client(holder.credentials)
                context.rest_client = rest
        except Exception:
            pass
    if cfg is None or rest is None:
        return None
    return cfg, rest


def try_heal_stuck_g4(*, min_elapsed_sec: float = _G4_HEAL_MIN_ELAPSED_SEC) -> bool:
    """Force-complete G4 when orchestrator is registered but the runner is wedged."""
    from system.boot.gate_sideband import gate_is_done

    state = get_system_state()
    if gate_is_done(state, "G4"):
        return False
    loops_built = _gate4_loops_built()
    if loops_built <= 0:
        return False
    snap = state.try_snapshot(timeout=0.25)
    if snap is not None:
        g4 = (snap.get("gates") or {}).get("G4") or {}
        status = str(g4.get("status") or "").lower()
        if status not in ("running", "complete"):
            return False
    started_mono = _gate4_started_mono
    if started_mono is not None:
        elapsed = time.monotonic() - started_mono
    elif snap is not None:
        epoch = float(snap.get("started_at_epoch") or 0)
        if epoch <= 0:
            return False
        elapsed = time.time() - epoch
    else:
        elapsed = float(min_elapsed_sec)
    if elapsed < float(min_elapsed_sec):
        return False
    from system.boot.gate_sideband import mark_gate_sideband

    mark_gate_sideband("G4")
    if not state._lock.acquire(timeout=_G4_HEAL_LOCK_TIMEOUT_SEC):
        log_engine("Gate4: boot heal sideband set — lock busy")
        return True
    try:
        if state._snapshot.gates["G4"].status == GateStatus.COMPLETE:
            return False
        state.update_state(
            BootPhase.G4,
            72,
            "Engines Armed (Array Warmup)",
            hydration={
                "ohlc_epics_ready": int((snap.get("hydration") or {}).get("ohlc_epics_ready") or 0),
                "ohlc_epics_total": loops_built,
            },
            loops={
                "built": loops_built,
                "running": True,
                "accepting_ticks": False,
            },
        )
        state.mark_gate_complete("G4", detail="boot_heal_orchestrator_ready")
        log_engine(f"Gate4: boot heal — {loops_built} loop(s) registered")
    finally:
        state._lock.release()
    return True


def _schedule_post_gate4_optional(
    *,
    cfg: Any,
    rest: Any,
    loops: list[Any],
    harness_mode: bool,
) -> None:
    """Target engine + Flight Deck can block on REST/SQLite — never hold G4 completion."""

    def _run() -> None:
        if not harness_mode and cfg.get("intelligence_layer", {}).get("enabled"):
            try:
                from intelligence.target_engine import initialize_target_engine

                store = getattr(loops[0], "_store", None) if loops else None
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

    threading.Thread(
        target=_run,
        name="gate4-post-optional",
        daemon=True,
    ).start()


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
        if _g4_should_exit(self._state):
            return
        if not _gate4_build_lock.acquire(timeout=120.0):
            log_engine("Gate4: build lock busy — exiting (watchdog/peer owns build)")
            return
        try:
            if _g4_should_exit(self._state):
                return
            note_g4_started()
            if self._state._lock.acquire(timeout=3.0):
                try:
                    self._state.update_state(
                        BootPhase.G4,
                        65,
                        "Loading Market Data",
                        gates_dict=None,
                    )
                finally:
                    self._state._lock.release()

            try:
                self._execute()
            except Exception as exc:
                message = f"Gate 4 hydration failed: {type(exc).__name__}: {exc}"
                log_engine(f"Gate4 FATAL: {message}")
                if self._state._lock.acquire(timeout=2.0):
                    try:
                        self._state.mark_gate_failed(
                            "G4",
                            error=message,
                            detail="OHLC bootstrap or loop construction failed",
                        )
                    finally:
                        self._state._lock.release()
        finally:
            _gate4_build_lock.release()

    def _execute(self) -> None:
        import os

        if _g4_should_exit(self._state):
            return
        resolved = _resolve_g4_boot_context(self._context)
        if resolved is None:
            raise RuntimeError("Gate 4 requires config and rest_client from prior gates")
        cfg, rest = resolved
        harness_mode = os.environ.get("IG_TEST_HARNESS", "").strip() == "1"

        def _optional_g4_preamble() -> None:
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

        threading.Thread(
            target=_optional_g4_preamble,
            name="gate4-optional-preamble",
            daemon=True,
        ).start()

        if _g4_should_exit(self._state):
            return

        try:
            orch = _build_orchestrator_off_thread(cfg, rest)
        except RuntimeError as exc:
            if "timed out" in str(exc):
                log_engine("Gate4: orchestrator build timed out — watchdog may force-build")
                return
            raise
        loops = list(orch.loops)
        total = len(loops)
        if total == 0:
            raise RuntimeError("No trading loops built for enabled instruments")

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

        _finalize_g4_complete(
            state=self._state,
            context=self._context,
            orch=orch,
            loops=loops,
            total=total,
            cfg=cfg,
            rest=rest,
            harness_mode=harness_mode,
            detail="orchestrator_registered",
        )
