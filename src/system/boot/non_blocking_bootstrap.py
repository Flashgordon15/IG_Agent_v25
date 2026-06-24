"""
Non-blocking boot lifecycle — bind :8080 first, hydrate gates in background.

Gate 2 (broker auth) and Gate 3 (reference price hydration) run off the main
thread with exponential backoff on rate-limit / timeout failures.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Callable

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_POST_BIND_THREAD: threading.Thread | None = None
_HYDRATION_THREAD: threading.Thread | None = None

GateRunner = Callable[[], None]

_DEFAULT_BACKOFF_SEC = 0.5
_MAX_BACKOFF_SEC = 30.0
_MAX_GATE_RETRIES = 12


def non_blocking_boot_enabled() -> bool:
    if os.environ.get("IG_BLOCKING_BOOT", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    if os.environ.get("IG_HARNESS_SYNC_BOOT", "").strip() == "1":
        return False
    return os.environ.get("IG_NON_BLOCKING_BOOT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def wait_for_api_port(
    host: str,
    port: int,
    *,
    timeout_sec: float = 5.0,
) -> bool:
    """Return True when TCP connect to the API port succeeds."""
    started = time.monotonic()
    deadline = started + float(timeout_sec)
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=0.15):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                log_engine(
                    f"non_blocking_boot: API port :{port} accepting ({elapsed_ms}ms)"
                )
                return True
        except OSError:
            time.sleep(0.01)
    return False


def run_gate_with_exponential_backoff(
    gate_id: str,
    runner: GateRunner,
    *,
    max_retries: int = _MAX_GATE_RETRIES,
) -> bool:
    """Run a gate runner with retry — never raises to the caller."""
    from system.system_state import get_system_state

    delay = _DEFAULT_BACKOFF_SEC
    for attempt in range(1, max_retries + 1):
        try:
            runner()
            if get_system_state().gate_complete(gate_id):
                return True
            snap = get_system_state().snapshot_model()
            detail = ""
            if snap.gates.get(gate_id):
                detail = str(snap.gates[gate_id].detail or "")
            log_engine(
                f"non_blocking_boot: {gate_id} attempt {attempt}/{max_retries} "
                f"incomplete ({detail or 'no detail'}) — retry in {delay:.1f}s"
            )
        except Exception as exc:
            log_guarded_exception(f"non_blocking_{gate_id}", exc)
            log_engine(
                f"non_blocking_boot: {gate_id} attempt {attempt}/{max_retries} "
                f"failed ({type(exc).__name__}) — retry in {delay:.1f}s"
            )
        if attempt >= max_retries:
            return False
        time.sleep(delay)
        delay = min(delay * 2.0, _MAX_BACKOFF_SEC)
    return False


class BackgroundGateHydrationWorker:
    """Gate 2 / Gate 3 hydration off the main thread with resilient retries."""

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="background-gate-hydration",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        from system.system_state import GATE_IDS

        coord = self._coordinator
        state = coord.state
        try:
            if not state.gate_complete("G1"):
                run_gate_with_exponential_backoff(
                    "G1",
                    coord.ensure_g1_complete,
                    max_retries=4,
                )
            for gate_id in ("G2", "G3"):
                if self._stop.is_set():
                    return
                previous = GATE_IDS[GATE_IDS.index(gate_id) - 1]
                if not state.gate_complete(previous):
                    log_engine(
                        f"non_blocking_boot: skipping {gate_id} — {previous} incomplete"
                    )
                    continue
                runner = coord._gate_runners.get(gate_id)
                if runner is None:
                    continue
                run_gate_with_exponential_backoff(
                    gate_id,
                    lambda r=runner, g=gate_id: coord._run_gate(g),
                )
            for gate_id in GATE_IDS[3:]:
                if self._stop.is_set():
                    return
                previous = GATE_IDS[GATE_IDS.index(gate_id) - 1]
                if not state.gate_complete(previous):
                    break
                coord._run_gate(gate_id)
                if coord._pipeline_failed():
                    break
        except Exception as exc:
            log_guarded_exception("background_gate_hydration", exc)


def schedule_post_bind_maintenance(
    *,
    boot_context: Any | None = None,
    purge_bytecode: bool = True,
    install_kernel: bool = True,
) -> None:
    """Bytecode purge + kernel interceptor — never on the critical bind path."""

    def _work() -> None:
        if purge_bytecode:
            try:
                import shutil
                from system.paths import project_root

                root = project_root()
                for pyc in root.rglob("*.pyc"):
                    try:
                        pyc.unlink()
                    except Exception:
                        pass
                for cache_dir in list(root.rglob("__pycache__")):
                    try:
                        shutil.rmtree(cache_dir)
                    except Exception:
                        pass
                log_engine("post_bind: workspace bytecode purged")
            except Exception as exc:
                log_guarded_exception("post_bind_bytecode_purge", exc)
        if install_kernel:
            try:
                from system.guard.kernel_interceptor import install_kernel_interceptor

                install_kernel_interceptor()
            except Exception as exc:
                log_guarded_exception("post_bind_kernel_interceptor", exc)
        if boot_context is None:
            try:
                from system.credentials_holder import bootstrap_credentials

                holder = bootstrap_credentials()
                if holder.credentials:
                    log_engine(
                        f"post_bind: credentials loaded ({holder.credentials.account_type})"
                    )
            except Exception as exc:
                log_guarded_exception("post_bind_credentials", exc)

    global _POST_BIND_THREAD
    if _POST_BIND_THREAD is not None and _POST_BIND_THREAD.is_alive():
        return
    _POST_BIND_THREAD = threading.Thread(
        target=_work,
        name="post-bind-maintenance",
        daemon=True,
    )
    _POST_BIND_THREAD.start()


def start_background_gate_hydration(coordinator: Any) -> BackgroundGateHydrationWorker:
    global _HYDRATION_THREAD
    worker = BackgroundGateHydrationWorker(coordinator)
    worker.start()
    _HYDRATION_THREAD = worker._thread
    return worker
