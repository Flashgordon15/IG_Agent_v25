"""Gate 5 — instant READY flip; array warmup and deploy verify run in background."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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

    def _execute(self) -> None:
        import os

        harness_mode = os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        for gate_id in _PRIOR_GATES:
            if not self._state.gate_complete(gate_id):
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

        unpause = getattr(orch, "unpause_from_boot", None)
        if callable(unpause):
            unpause()
        else:
            for loop in getattr(orch, "loops", []) or []:
                fn = getattr(loop, "unpause_from_boot", None)
                if callable(fn):
                    fn()

        snap = self._state.snapshot_model()
        self._state.update_state(
            BootPhase.G5,
            98,
            "ACTIVE",
            gates_dict=None,
            loops={
                "built": snap.loops.built,
                "running": True,
                "accepting_ticks": True,
            },
        )

        from system.boot.post_ready_services import start_post_ready_services

        start_post_ready_services(self._context)
        if not harness_mode:
            _spawn_background_deploy_verify(self._state)

        self._state.set_ready(label="ACTIVE")
        try:
            from system.agent_execution_mode import ensure_demo_sandbox_execution_armed

            ensure_demo_sandbox_execution_armed()
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
