"""Abort startup if :8080 never binds — prevents silent pre-uvicorn hangs."""

from __future__ import annotations

import os
import socket
import threading
import time

from system.engine_log import log_engine

_watchdog_started = False


def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def start_pre_bind_watchdog(*, port: int, host: str = "127.0.0.1") -> None:
    """Daemon thread — exit process if API port not listening within deadline."""
    global _watchdog_started
    if os.environ.get("IG_AGENT_PYTEST") == "1" or os.environ.get("IG_TEST_HARNESS") == "1":
        return
    if os.environ.get("IG_PRE_BIND_WATCHDOG", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    if _watchdog_started:
        return
    _watchdog_started = True
    timeout_sec = float(os.environ.get("IG_PRE_BIND_WATCHDOG_SEC", "90"))

    def _run() -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if _port_listening(host, port):
                log_engine(
                    f"boot_watchdog: :{port} listening "
                    f"({int((timeout_sec - (deadline - time.monotonic())) * 1000)}ms)"
                )
                return
            time.sleep(2.0)
        log_engine(
            f"boot_watchdog: FATAL — :{port} not listening within {timeout_sec:.0f}s "
            "(pre-bind stall; exit for supervisor retry)"
        )
        os._exit(3)

    threading.Thread(target=_run, name="pre-bind-watchdog", daemon=True).start()
