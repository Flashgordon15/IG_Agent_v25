"""In-agent fallback — gate coherence every 6 hours (4×/day) when launchd is absent."""

from __future__ import annotations

import os
import threading

from system.engine_log import log_engine

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_INTERVAL_SEC = 6 * 3600


def _should_run() -> bool:
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return False
    if os.environ.get("IG_AGENT_GATE_COHERENCE_SCHED", "").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return False
    return True


def _run_once() -> None:
    try:
        from system.gate_coherence import run_scheduled_coherence_check

        run_scheduled_coherence_check(repair_db=False, alert_on_critical=True)
    except Exception as e:
        log_engine(f"gate_coherence_scheduler error: {type(e).__name__}: {e}")


def start_gate_coherence_scheduler() -> None:
    """Daemon thread: per-market alignment check every 6 hours."""
    global _THREAD
    if not _should_run():
        return
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()

    def _loop() -> None:
        while not _STOP.wait(_INTERVAL_SEC):
            _run_once()

    _THREAD = threading.Thread(
        target=_loop, name="gate-coherence-scheduler", daemon=True
    )
    _THREAD.start()
    log_engine("gate_coherence_scheduler started (every 6h, per-market)")


def stop_gate_coherence_scheduler() -> None:
    _STOP.set()


def reset_gate_coherence_scheduler_for_tests() -> None:
    stop_gate_coherence_scheduler()
    global _THREAD
    _THREAD = None
    _STOP.clear()
