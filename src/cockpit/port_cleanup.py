"""Flight Deck cockpit — native tkinter avionics HUD (isolated process)."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

DEFAULT_PORT = 8080


def clear_port_8080(*, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> list[int]:
    """
    Terminate processes listening on the dashboard port.

    Returns list of PIDs sent SIGTERM. Uses TERM only (no kill -9).
    """
    killed: list[int] = []
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return killed

    for line in out.strip().splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 15)
            killed.append(pid)
        except OSError:
            pass
    if killed:
        time.sleep(0.4)
    return killed
