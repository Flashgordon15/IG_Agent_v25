"""
Runtime control hooks — trading loop start/stop from FastAPI (Step 13).
"""

from __future__ import annotations

import subprocess
import threading
from typing import Any

from system.engine_log import log_engine
from system.paths import project_root

_loop: Any | None = None
_paused: bool = False
_lock = threading.Lock()


def register_trading_loop(loop: Any | None) -> None:
    global _loop
    with _lock:
        _loop = loop


def is_paused() -> bool:
    with _lock:
        return _paused


def start_trading() -> dict[str, Any]:
    global _paused
    with _lock:
        _paused = False
        loop = _loop
    if loop is None:
        return {"ok": False, "error": "trading loop not registered"}
    if loop.is_running():
        return {"ok": True, "status": "already_running"}
    loop.start()
    log_engine("api: trading loop started")
    return {"ok": True, "status": "started"}


def stop_trading() -> dict[str, Any]:
    global _paused
    with _lock:
        _paused = True
        loop = _loop
    if loop is None:
        return {"ok": False, "error": "trading loop not registered"}
    if not loop.is_running():
        return {"ok": True, "status": "already_stopped"}
    loop.stop()
    log_engine("api: trading loop stopped")
    return {"ok": True, "status": "stopped"}


def run_emergency_stop() -> dict[str, Any]:
    script = project_root() / "scripts" / "emergency_stop.sh"
    if not script.is_file():
        return {"ok": False, "error": f"missing {script}"}
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script)],
            cwd=str(project_root()),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
