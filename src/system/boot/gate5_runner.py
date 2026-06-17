"""Gate 5 — atomic READY flip, unpause dormant loops, background deploy verify."""

from __future__ import annotations

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
            log_engine(f"Gate5: background deploy verify error: {type(exc).__name__}: {exc}")
        _update_pytest_status(state, pytest_status=status, last_run_at=_utc_now_iso())

    threading.Thread(
        target=_worker,
        name="background-deploy-verify",
        daemon=True,
    ).start()


class Gate5Runner:
    """Verify prior gates, unpause trading loops, flip SystemState.ready."""

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
        for gate_id in _PRIOR_GATES:
            if not self._state.gate_complete(gate_id):
                raise RuntimeError(f"{gate_id} not COMPLETE — cannot activate")

        orch = self._context.orchestrator
        if orch is None:
            raise RuntimeError("Gate 5 requires orchestrator from Gate 4")

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
        _spawn_background_deploy_verify(self._state)

        self._state.set_ready(label="ACTIVE")
        try:
            from system.diagnostics.perf_metrics import start_disk_flush_after_ready

            start_disk_flush_after_ready()
        except Exception:
            pass
        log_engine(
            f"Gate5: SystemState READY — {snap.loops.built} loop(s) accepting live ticks"
        )
