"""In-agent feed guardian — auto-heal when velocity stall persists after boot."""

from __future__ import annotations

import os
import threading
import time

from system.engine_log import log_engine

_GUARDIAN_THREAD: threading.Thread | None = None
_GUARDIAN_STOP = threading.Event()
_INTERVAL_SEC = 5.0


def _guardian_loop() -> None:
    _dual_core_tpm_zero_since: float = 0.0
    while not _GUARDIAN_STOP.wait(_INTERVAL_SEC):
        try:
            from runtime.dual_core_execution import (
                TPM_ZERO_REHYDRATE_SEC,
                _ticks_per_minute,
                get_active_stack_epics,
                _trigger_non_blocking_stream_rehydration,
            )

            stack = get_active_stack_epics()
            if stack and all(_ticks_per_minute(e) == 0 for e in stack):
                now = time.time()
                if _dual_core_tpm_zero_since <= 0.0:
                    _dual_core_tpm_zero_since = now
                elif (now - _dual_core_tpm_zero_since) >= TPM_ZERO_REHYDRATE_SEC:
                    _trigger_non_blocking_stream_rehydration(list(stack))
                    log_engine(
                        "agent_feed_guardian: dual-core tpm=0 — stream rehydrate triggered"
                    )
                    _dual_core_tpm_zero_since = now
            else:
                _dual_core_tpm_zero_since = 0.0
        except Exception as exc:
            log_engine(f"agent_feed_guardian: dual-core check {type(exc).__name__}: {exc}")

        try:
            from system.unified_fulfillment_cache import get_fulfillment_payload

            payload = get_fulfillment_payload()
            dv = payload.get("data_velocity") or {}
            if not bool(dv.get("stall_active")):
                continue
            frozen = float(dv.get("frozen_sec") or 0)
            if frozen < 8.0:
                continue
            from system.unified_fulfillment_cache import force_cockpit_feed_heal

            result = force_cockpit_feed_heal(reason="agent_guardian")
            log_engine(
                f"agent_feed_guardian: heal triggered frozen={frozen:.1f}s "
                f"ok={result.get('ok')}"
            )
        except Exception as exc:
            log_engine(f"agent_feed_guardian: {type(exc).__name__}: {exc}")


def start_agent_feed_guardian() -> None:
    global _GUARDIAN_THREAD
    if os.environ.get("IG_AGENT_FEED_GUARDIAN", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    if _GUARDIAN_THREAD is not None and _GUARDIAN_THREAD.is_alive():
        return
    _GUARDIAN_STOP.clear()
    _GUARDIAN_THREAD = threading.Thread(
        target=_guardian_loop,
        name="agent-feed-guardian",
        daemon=True,
    )
    _GUARDIAN_THREAD.start()
    log_engine("agent_feed_guardian: started (5s poll, heal at 8s stall)")


def stop_agent_feed_guardian() -> None:
    _GUARDIAN_STOP.set()


def reset_agent_feed_guardian_for_tests() -> None:
    global _GUARDIAN_THREAD
    stop_agent_feed_guardian()
    _GUARDIAN_THREAD = None
