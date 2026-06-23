"""
Clean shutdown sequence — crash_state.json + ordered teardown hooks.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir

_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_REQUESTED = False
_CRASH_STATE = data_dir() / "crash_state.json"


def write_crash_state(
    *,
    source: str,
    exc: BaseException | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "pid": os.getpid(),
        "agent_mode": os.environ.get("IG_AGENT_MODE", ""),
        "exception": None if exc is None else f"{type(exc).__name__}: {exc}",
        "traceback": None if exc is None else traceback.format_exc(),
    }
    if extra:
        payload.update(extra)
    try:
        _CRASH_STATE.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_STATE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log_engine(f"crash_state written path={_CRASH_STATE} source={source}")
    except Exception as write_exc:
        log_guarded_exception("crash_state_write", write_exc)
    return _CRASH_STATE


def perform_iron_clad_shutdown(
    *,
    source: str = "signal",
    rest_client: Any = None,
) -> None:
    """
    Ordered shutdown per Module 1:
    a) halt ML ingestion
    b) cancel working orders
    c) close bot positions
    d) disconnect streams
    e) crash_state.json
    """
    global _SHUTDOWN_REQUESTED
    with _SHUTDOWN_LOCK:
        if _SHUTDOWN_REQUESTED:
            return
        _SHUTDOWN_REQUESTED = True

    log_engine(f"iron_clad_shutdown: begin source={source}")

    try:
        from system.unified_fulfillment_cache import stop_fulfillment_cache_refresh

        stop_fulfillment_cache_refresh()
        log_engine("iron_clad_shutdown: fulfillment cache stopped")
    except Exception as exc:
        log_guarded_exception("iron_clad_shutdown_ml", exc)

    client = rest_client
    if client is None:
        try:
            from ig_api.rest_client import get_shared_rest_client

            client = get_shared_rest_client()
        except Exception:
            client = None

    if client is not None:
        try:
            from execution.capital_guard import CapitalGuard

            CapitalGuard._cancel_all_open_orders_and_positions(client)
            log_engine("iron_clad_shutdown: orders cancelled / positions flattened")
        except Exception as exc:
            log_guarded_exception("iron_clad_shutdown_flatten", exc)

    try:
        from runtime.agent_bootstrap import stop_market_stream

        stop_market_stream()
        log_engine("iron_clad_shutdown: market stream stopped")
    except Exception as exc:
        log_guarded_exception("iron_clad_shutdown_stream", exc)

    try:
        from system.unified_fulfillment_cache import stop_fulfillment_cache_refresh

        stop_fulfillment_cache_refresh()
    except Exception:
        pass

    write_crash_state(source=source, extra={"shutdown_complete": True})
    log_engine("iron_clad_shutdown: complete")


def install_signal_handlers(rest_client: Any | None = None) -> None:
    """Bind SIGINT/SIGTERM to iron-clad shutdown (idempotent)."""

    def _handler(signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        perform_iron_clad_shutdown(source=sig_name, rest_client=rest_client)
        try:
            from system.shutdown_cleanup import perform_shutdown_cleanup

            perform_shutdown_cleanup(source=sig_name)
        except Exception:
            pass
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except Exception:
            pass


def reset_shutdown_state_for_tests() -> None:
    global _SHUTDOWN_REQUESTED
    with _SHUTDOWN_LOCK:
        _SHUTDOWN_REQUESTED = False
