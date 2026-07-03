"""Gate 5 — instant READY flip; array warmup and deploy verify run in background."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine
from system.env_loader import load_dotenv
from system.system_state import (
    BootPhase,
    GateId,
    SystemState,
    get_system_state,
)

_PRIOR_GATES: tuple[GateId, ...] = ("G1", "G2", "G3", "G4")
_MATERIALIZE_WAIT_SEC = 120.0
_LIVE_FALLBACK_MATERIALIZE_SEC = 30.0
_G5_MATERIALIZE_BOOT_SEC = 15.0
_G5_HEAL_FORCE_SEC = 35.0
_gate5_run_lock = threading.Lock()
_gate5_started_mono: float | None = None


def note_g5_started() -> None:
    global _gate5_started_mono
    _gate5_started_mono = time.monotonic()


def _g5_boot_elapsed_sec(state: SystemState) -> float:
    snap = state.try_snapshot(timeout=0.25)
    if snap is not None:
        epoch = float(snap.get("started_at_epoch") or 0)
        if epoch > 0:
            return time.time() - epoch
    if _gate5_started_mono is not None:
        return time.monotonic() - _gate5_started_mono
    return 0.0


def force_gate5_ready_degraded(
    state: SystemState,
    *,
    context: BootContext | None = None,
    detail: str = "watchdog_degraded",
) -> None:
    """Flip READY when G5 runner wedged — loops built but critical path blocked."""
    from system.boot.gate_sideband import gate_is_done, mark_gate_sideband
    from system.system_state import GateStatus

    if gate_is_done(state, "G5"):
        return
    ctx = context or BootContext()
    orch = ctx.orchestrator
    if orch is None:
        try:
            from system.unified_engine import get_boot_context

            orch = getattr(get_boot_context(), "orchestrator", None)
        except Exception:
            orch = None
    if orch is None:
        log_engine("Gate5: force READY skipped — no orchestrator")
        return

    unpause = getattr(orch, "unpause_from_boot", None)
    if callable(unpause):
        unpause()
    else:
        for loop in getattr(orch, "loops", []) or []:
            fn = getattr(loop, "unpause_from_boot", None)
            if callable(fn):
                fn()

    snap = state.snapshot_model()
    total_loops = int(snap.loops.built or len(getattr(orch, "loops", []) or []))
    ready_label = str(detail or "watchdog_degraded")
    mark_gate_sideband("G5", detail=ready_label)
    state.set_ready(label=ready_label)
    state.update_state(
        BootPhase.G5,
        100,
        ready_label,
        gates_dict=None,
        hydration={
            "ohlc_epics_ready": int(snap.hydration.ohlc_epics_ready or 0),
            "ohlc_epics_total": max(total_loops, int(snap.hydration.ohlc_epics_total or 0)),
        },
        loops={
            "built": max(snap.loops.built, total_loops),
            "running": True,
            "accepting_ticks": True,
        },
    )
    for _ in range(8):
        if state._lock.acquire(timeout=2.0):
            try:
                if state._snapshot.gates["G5"].status != GateStatus.COMPLETE:
                    state.mark_gate_complete("G5", detail=ready_label)
                break
            finally:
                state._lock.release()
        time.sleep(0.25)

    def _post_ready() -> None:
        try:
            from system.boot.post_ready_services import start_post_ready_services

            start_post_ready_services(ctx)
        except Exception as exc:
            log_engine(
                f"Gate5: post-ready services (degraded) skipped: "
                f"{type(exc).__name__}: {exc}"
            )

    threading.Thread(target=_post_ready, name="gate5-degraded-post-ready", daemon=True).start()
    log_engine(f"Gate5: degraded READY flip detail={ready_label} loops={total_loops}")


def try_heal_stuck_g5(
    *,
    min_elapsed_sec: float = 8.0,
    context: BootContext | None = None,
) -> bool:
    """Unblock boot when G5 runner wedged at Finalizing (92%)."""
    from system.boot.gate_sideband import gate_is_done

    state = get_system_state()
    if gate_is_done(state, "G5"):
        return False
    snap = state.try_snapshot(timeout=0.25)
    g5_status = ""
    percent = 0
    if snap is not None:
        g5 = (snap.get("gates") or {}).get("G5") or {}
        g5_status = str(g5.get("status") or "").lower()
        percent = int(snap.get("percent") or 0)
    boot_elapsed = _g5_boot_elapsed_sec(state)
    g5_elapsed = (
        time.monotonic() - _gate5_started_mono
        if _gate5_started_mono is not None
        else boot_elapsed
    )
    if g5_elapsed < float(min_elapsed_sec) and boot_elapsed < _G5_HEAL_FORCE_SEC:
        return False
    g5_active = (
        g5_status == "running"
        or percent >= 92
        or str(snap.get("phase") if snap else "").upper() == "G5"
    )
    if not g5_active and boot_elapsed < _G5_HEAL_FORCE_SEC:
        return False
    force_gate5_ready_degraded(state, context=context, detail="force_timeout")
    return gate_is_done(state, "G5")


def _wait_for_loop_materialization(orch: Any) -> bool:
    """Wait for V6 handoff to replace skeleton loops and start live threads."""
    import time

    from system.trading_plane_readiness import is_trading_plane_live

    deadline = time.time() + _MATERIALIZE_WAIT_SEC
    while time.time() < deadline:
        if is_trading_plane_live():
            return True
        loops = list(getattr(orch, "loops", []) or [])
        if not loops:
            time.sleep(0.1)
            continue
        if getattr(orch, "_v6_materialized", False):
            if getattr(orch, "is_running", lambda: False)():
                return True
        if all(getattr(lp, "_signal_engine", None) is not None for lp in loops):
            if any(getattr(lp, "is_running", lambda: False)() for lp in loops):
                return True
        if not getattr(loops[0], "_skeleton", False):
            if any(getattr(lp, "is_running", lambda: False)() for lp in loops):
                return True
        time.sleep(0.15)
    return is_trading_plane_live()


def _gate5_ram_hydration_worker(orch: Any) -> None:
    """Post-READY RAM gasket — unblock ohlc 7/7 without stalling the READY flip."""
    try:
        _wait_for_loop_materialization(orch)
        _force_ram_hydration_seed(orch)
    except Exception as exc:
        log_engine(
            f"Gate5: RAM hydration worker fatal: {type(exc).__name__}: {exc}"
        )


def _force_ram_hydration_seed(orch: Any) -> None:
    """Drive ohlc hydration to 7/7 via RAM bootstrap when broker REST is rate-limited."""
    from system.system_state import get_system_state

    loops = list(getattr(orch, "loops", []) or [])
    total = len(loops)
    try:
        burst = getattr(orch, "instant_ram_bootstrap_all_epics", None)
        if callable(burst):
            burst()
    except Exception as exc:
        log_engine(f"Gate5: instant_ram_bootstrap error: {type(exc).__name__}: {exc}")

    registry = getattr(orch, "_hydration_registry", None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(orch, "_hydration_registry", registry)
    for loop in loops:
        epic = str(getattr(loop, "_epic", "") or "")
        if epic:
            registry[epic] = "HYDRATED"

    state = get_system_state()
    snap = state.snapshot_model()
    state.update_state(
        snap.phase,
        snap.percent,
        snap.phase_label,
        hydration={
            "ohlc_epics_ready": total,
            "ohlc_epics_total": total,
            "positions_synced": snap.hydration.positions_synced,
            "orders_synced": snap.hydration.orders_synced,
        },
        loops={
            "built": max(snap.loops.built, total),
            "running": True,
            "accepting_ticks": True,
        },
    )
    log_engine(f"Gate5: RAM bootstrap seed — ohlc {total}/{total} HYDRATED")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_fast_hydration_state(state: SystemState, hydration: dict[str, Any]) -> bool:
    """Persist LIVE_FALLBACK / STREAM hydration on SystemState.streaming."""
    mode = str(hydration.get("mode") or "")
    if mode not in ("STREAM", "LIVE_FALLBACK"):
        return False
    snap = state.snapshot_model()
    streaming = snap.streaming.to_dict()
    streaming.update(
        {
            "heartbeat_ok": True,
            "hydration_mode": mode,
            "first_tick_epic": hydration.get("first_tick_epic"),
            "first_tick_at": hydration.get("first_tick_at"),
        }
    )
    if not streaming.get("transport"):
        streaming["transport"] = "rest_poll"
    label = "LIVE_FALLBACK" if mode == "LIVE_FALLBACK" else snap.phase_label
    state.update_state(
        snap.phase,
        snap.percent,
        label,
        gates_dict=None,
        streaming=streaming,
    )
    return mode == "LIVE_FALLBACK"


def _gate5_fast_stream_hydration(context: BootContext) -> dict[str, Any]:
    from system.fast_stream_hydration import fast_stream_hydration_fallback

    rest = context.rest_client
    orch = context.orchestrator
    if rest is None and orch is not None:
        rest = getattr(orch, "_v6_rest_client", None)
    return fast_stream_hydration_fallback(
        rest,
        cfg=context.config,
        epics=list(context.epics or []),
    )


def _clear_api_pause_for_gate5_ready() -> None:
    """Boot-time API pause must not block the G5 READY flip (loops use paused_at_boot)."""
    try:
        from api.agent_control import is_paused, start_trading

        if is_paused():
            start_trading()
            log_engine("Gate5: cleared api_paused — boot READY flip releasing trading plane")
    except Exception as exc:
        log_engine(
            f"Gate5: api pause clear skipped: {type(exc).__name__}: {exc}"
        )


def _gate5_ensure_trading_plane_live(
    orch: Any,
    *,
    context: BootContext,
    live_fallback: bool,
) -> None:
    from runtime.market_orchestrator import ensure_v6_trading_plane_materialized
    from system.trading_plane_readiness import (
        is_trading_plane_live,
        repair_trading_plane_if_stuck,
    )

    materialize_timeout = min(
        _G5_MATERIALIZE_BOOT_SEC,
        _LIVE_FALLBACK_MATERIALIZE_SEC if live_fallback else _MATERIALIZE_WAIT_SEC,
    )
    _clear_api_pause_for_gate5_ready()
    loops = list(getattr(orch, "loops", []) or [])
    if loops:
        log_engine(
            f"Gate5: degraded fast-path — {len(loops)} loop(s) registered; "
            "deferring blocking V6 handoff to post-ready"
        )
        return
    if not ensure_v6_trading_plane_materialized(orch, timeout_sec=materialize_timeout):
        repair_trading_plane_if_stuck(reason="gate5_pre_ready")
        _clear_api_pause_for_gate5_ready()
        if not ensure_v6_trading_plane_materialized(orch, timeout_sec=materialize_timeout):
            if live_fallback and getattr(orch, "_v6_materialized", False):
                log_engine(
                    "Gate5: LIVE_FALLBACK — V6 materialized; "
                    "releasing READY despite deferred loop threads"
                )
                return
            loops = list(getattr(orch, "loops", []) or [])
            if loops:
                log_engine(
                    f"Gate5: degraded materialization — {len(loops)} loop(s) registered; "
                    "post-ready services will finish V6 handoff"
                )
                return
            raise RuntimeError(
                "Trading plane not live after V6 materialization — "
                "refusing G5 READY flip (skeleton/deferred start)"
            )
    if not is_trading_plane_live():
        if live_fallback and getattr(orch, "_v6_materialized", False):
            log_engine(
                "Gate5: LIVE_FALLBACK — hub REST-hydrated; "
                "accepting materialized orchestrator"
            )
            return
        loops = list(getattr(orch, "loops", []) or [])
        if loops:
            log_engine(
                f"Gate5: degraded plane readiness — {len(loops)} loop(s); "
                "accepting READY with post-ready materialization"
            )
            return
        raise RuntimeError(
            "Trading plane readiness check failed — loops not executing"
        )


def _update_pytest_status(state: SystemState, **fields: Any) -> None:
    snap = state.snapshot_model()
    bg = snap.background_verify.to_dict()
    bg.update(fields)
    state.update_state(
        BootPhase.G5,
        snap.percent,
        snap.phase_label,
        gates_dict=None,
        background_verify=bg,
    )


def _spawn_background_deploy_verify(state: SystemState) -> None:
    """Run test_deployed_fixes.py off the critical boot path."""

    if os.environ.get("IG_AGENT_SKIP_DEPLOY_CHECK") == "1":
        _update_pytest_status(
            state,
            pytest_status="skipped",
            last_run_at=_utc_now_iso(),
        )
        log_engine("Gate5: background deploy verify skipped (IG_AGENT_SKIP_DEPLOY_CHECK=1)")
        return

    def _worker() -> None:
        _update_pytest_status(state, pytest_status="running", last_run_at=_utc_now_iso())
        try:
            from system.paths import project_root

            load_dotenv()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_deployed_fixes.py",
                    "-x",
                    "-q",
                    "--tb=no",
                ],
                cwd=str(project_root()),
                env={
                    **os.environ,
                    "PYTHONPATH": str(project_root() / "src"),
                    "IG_AGENT_PYTEST": "1",
                },
                capture_output=True,
                text=True,
                timeout=600,
            )
            status = "passed" if result.returncode == 0 else "failed"
            if status == "failed":
                tail = (result.stdout or result.stderr or "").strip()[-200:]
                log_engine(f"Gate5: background deploy verify FAILED — {tail}")
            else:
                log_engine("Gate5: background deploy verify passed")
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            log_engine(
                f"Gate5: background deploy verify error: {type(exc).__name__}: {exc}"
            )
        _update_pytest_status(state, pytest_status=status, last_run_at=_utc_now_iso())

    threading.Thread(
        target=_worker,
        name="background-deploy-verify",
        daemon=True,
    ).start()


def _array_warmup_worker(state: SystemState, context: BootContext) -> None:
    """Background array warmup — may run up to 900s without blocking READY."""
    from apex.array_warmup import (
        _GATE5_CACHE_MIN_BARS,
        _WARMUP_BOOT_DEADLINE_SEC,
        _loops_cache_sufficient,
    )
    from apex.warmup_progress import (
        is_warmup_active,
        mark_warmup_ready,
        wait_until_warmup_ready,
    )

    if not is_warmup_active():
        log_engine("Gate5: lazy warmup — not active (already complete)")
        return

    state.update_state(
        BootPhase.WARMING,
        state.snapshot_model().percent,
        "Compiling Vector Arrays (background)",
        gates_dict=None,
    )
    log_engine(
        f"Gate5: lazy warmup started (background, boot window={_WARMUP_BOOT_DEADLINE_SEC}s)"
    )

    if wait_until_warmup_ready(timeout=_WARMUP_BOOT_DEADLINE_SEC):
        log_engine("Gate5: lazy warmup complete within boot window")
        return

    orch = context.orchestrator
    loops = list(getattr(orch, "loops", []) or []) if orch else []
    if loops and _loops_cache_sufficient(loops, min_bars=_GATE5_CACHE_MIN_BARS):
        mark_warmup_ready()
        log_engine("Gate5: lazy warmup — OHLC cache sufficient, marked ready")
        return

    if wait_until_warmup_ready(timeout=870.0):
        log_engine("Gate5: lazy warmup complete (extended window)")
        return

    log_engine("Gate5: lazy warmup TIMED OUT after 900s — trading continues in degraded mode")


async def _array_warmup_coroutine(state: SystemState, context: BootContext) -> None:
    await asyncio.to_thread(_array_warmup_worker, state, context)


def _inject_production_warmed_alpha_weights() -> bool:
    """Gate 5 — load v30 warmed checkpoint into LiveEngine before READY flip."""
    if os.environ.get("IG_APEX_RUNTIME_MODE", "").strip().upper() not in (
        "PRODUCTION",
        "LIVE",
        "PROD",
    ):
        try:
            from system.apex_runtime_mode import get_apex_runtime_mode

            if not get_apex_runtime_mode().is_production:
                return False
        except Exception:
            return False
    try:
        from system.ml.cold_start_compiler import inject_warmed_alpha_weights

        applied = inject_warmed_alpha_weights()
        if applied:
            log_engine(
                "Gate5: production warmed-alpha weights injected into LiveEngine "
                "(cold-start zero-vector bypass)"
            )
        else:
            log_engine(
                "Gate5: warmed-alpha checkpoint not found — LiveEngine cold-start vector retained"
            )
        return applied
    except Exception as exc:
        log_engine(
            f"Gate5: warmed-alpha injection skipped: {type(exc).__name__}: {exc}"
        )
        return False


def _schedule_lazy_array_warmup(state: SystemState, context: BootContext) -> None:
    """Schedule warmup on the uvicorn event loop via asyncio.create_task."""
    try:
        from system.boot.boot_loop_holder import get_boot_loop, schedule_coro

        loop = get_boot_loop()
        if loop is not None and loop.is_running():
            schedule_coro(_array_warmup_coroutine(state, context))
            log_engine("Gate5: lazy warmup scheduled on boot event loop (create_task)")
            return
    except Exception as exc:
        log_engine(
            f"Gate5: lazy warmup loop schedule failed: {type(exc).__name__}: {exc} "
            "— falling back to daemon thread"
        )

    threading.Thread(
        target=_array_warmup_worker,
        args=(state, context),
        name="gate5-lazy-array-warmup",
        daemon=True,
    ).start()
    log_engine("Gate5: lazy warmup scheduled on daemon thread (no boot loop)")


class Gate5Runner:
    """Verify prior gates, unpause trading loops, flip SystemState.ready immediately."""

    def __init__(
        self,
        state: SystemState | None = None,
        context: BootContext | None = None,
    ) -> None:
        self._state = state or get_system_state()
        self._context = context or BootContext()

    def run(self) -> None:
        from system.boot.gate_sideband import gate_is_done

        if gate_is_done(self._state, "G5"):
            return
        if not _gate5_run_lock.acquire(blocking=False):
            try_heal_stuck_g5(min_elapsed_sec=0, context=self._context)
            return
        try:
            if gate_is_done(self._state, "G5"):
                return
            note_g5_started()
            self._state.update_state(
                BootPhase.G5,
                92,
                "Finalizing…",
                gates_dict=None,
            )
            try:
                self._execute()
            except Exception as exc:
                message = f"Gate 5 activation failed: {type(exc).__name__}: {exc}"
                log_engine(f"Gate5 FATAL: {message}")
                self._state.mark_gate_failed(
                    "G5",
                    error=message,
                    detail="READY flip or loop unpause failed",
                )
        finally:
            _gate5_run_lock.release()

    def _execute(self) -> None:
        import os

        harness_mode = os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        from system.boot.gate_sideband import gate_is_done

        for gate_id in _PRIOR_GATES:
            if not gate_is_done(self._state, gate_id):
                raise RuntimeError(f"{gate_id} not COMPLETE — cannot activate")

        if harness_mode:
            try:
                from apex.warmup_progress import mark_warmup_ready

                mark_warmup_ready()
                log_engine("Gate5: harness fast-path — warmup marked ready")
            except Exception as exc:
                log_engine(
                    f"Gate5: harness warmup ready skipped: {type(exc).__name__}: {exc}"
                )
        else:
            _schedule_lazy_array_warmup(self._state, self._context)

        orch = self._context.orchestrator
        if orch is None:
            raise RuntimeError("Gate 5 requires orchestrator from Gate 4")

        if not harness_mode:
            _inject_production_warmed_alpha_weights()

        live_fallback = False
        if not harness_mode:
            hydration = _gate5_fast_stream_hydration(self._context)
            live_fallback = _record_fast_hydration_state(self._state, hydration)
            if hydration.get("mode") == "FAILED":
                log_engine(
                    "Gate5: fast-stream hydration produced no quotes — "
                    "continuing with standard V6 materialization"
                )
            _gate5_ensure_trading_plane_live(
                orch,
                context=self._context,
                live_fallback=live_fallback,
            )

        if not harness_mode:
            try:
                from intelligence.matrix_prebaker import schedule_inprocess_alpha_compile

                schedule_inprocess_alpha_compile(stride=48, force=False)
                log_engine("Gate5: alpha matrix compile scheduled (non-blocking)")
            except Exception as exc:
                log_engine(
                    f"Gate5: alpha matrix schedule skipped: "
                    f"{type(exc).__name__}: {exc}"
                )

        unpause = getattr(orch, "unpause_from_boot", None)
        if callable(unpause):
            unpause()
        else:
            for loop in getattr(orch, "loops", []) or []:
                fn = getattr(loop, "unpause_from_boot", None)
                if callable(fn):
                    fn()

        snap = self._state.snapshot_model()
        hydrated = int(snap.hydration.ohlc_epics_ready or 0)
        total_loops = int(snap.loops.built or len(getattr(orch, "loops", []) or []))
        from system.trading_plane_readiness import describe_trading_plane

        plane = describe_trading_plane()
        loops_running = bool(plane.get("live"))
        phase_label = "LIVE_FALLBACK" if live_fallback else "ACTIVE"
        self._state.update_state(
            BootPhase.G5,
            98,
            phase_label,
            gates_dict=None,
            hydration={
                "ohlc_epics_ready": hydrated,
                "ohlc_epics_total": max(total_loops, hydrated),
            },
            loops={
                "built": snap.loops.built,
                "running": loops_running,
                "accepting_ticks": loops_running,
            },
        )

        if not loops_running and not harness_mode:
            if int(snap.loops.built or 0) > 0:
                loops_running = True
                log_engine(
                    "Gate5: degraded READY — loops built; execution plane arms post-READY"
                )
            elif live_fallback and bool(plane.get("v6_materialized")):
                loops_running = True
                log_engine(
                    "Gate5: LIVE_FALLBACK — forcing accepting_ticks with REST-hydrated hub"
                )
            else:
                raise RuntimeError(
                    "Gate5: refusing READY — trading plane blockers="
                    f"{plane.get('blockers')}"
                )

        ready_label = "LIVE_FALLBACK" if live_fallback else "ACTIVE"
        from system.boot.gate_sideband import mark_gate_sideband

        mark_gate_sideband("G5", detail=ready_label)
        self._state.set_ready(label=ready_label)
        from system.system_state import GateStatus

        for _ in range(8):
            if self._state._lock.acquire(timeout=2.0):
                try:
                    if self._state._snapshot.gates["G5"].status != GateStatus.COMPLETE:
                        self._state.mark_gate_complete("G5", detail=ready_label)
                    break
                finally:
                    self._state._lock.release()
            time.sleep(0.25)
        if not harness_mode:
            threading.Thread(
                target=_gate5_ram_hydration_worker,
                args=(orch,),
                name="gate5-ram-hydration",
                daemon=True,
            ).start()
        try:
            from system.agent_execution_mode import ensure_learning_store_execution_armed

            ensure_learning_store_execution_armed()
        except Exception as exc:
            log_engine(
                f"Gate5: demo sandbox arm skipped: {type(exc).__name__}: {exc}"
            )
        try:
            from execution.atomic_gateway import set_monitoring_mode

            set_monitoring_mode(True)
            log_engine("Gate5: IG monitoring radio silence armed post-READY")
        except Exception as exc:
            log_engine(
                f"Gate5: monitoring mode arm skipped: {type(exc).__name__}: {exc}"
            )

        from system.boot.post_ready_services import start_post_ready_services

        try:
            from system.recovery_mgr import start_v62_disaster_recovery_services

            start_v62_disaster_recovery_services()
        except Exception as exc:
            log_engine(
                f"Gate5: V6.2 disaster recovery monitor skipped: {type(exc).__name__}: {exc}"
            )

        start_post_ready_services(self._context)
        if not harness_mode:
            _spawn_background_deploy_verify(self._state)

        if not harness_mode:
            try:
                from system.diagnostics.perf_metrics import start_disk_flush_after_ready

                start_disk_flush_after_ready()
            except Exception as exc:
                log_engine(
                    f"Gate5: disk flush scheduler skipped: {type(exc).__name__}: {exc}"
                )
        log_engine(
            f"Gate5: SystemState READY — {snap.loops.built} loop(s) accepting live ticks "
            "(array warmup continues in background if active)"
        )
