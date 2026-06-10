"""
Runtime control hooks — trading loop start/stop from FastAPI (Step 13).

Dashboard WebSocket ticks attach supervision_drift via cached runtime fields
from api.agent_health (refreshed in the health-cache background thread).
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


def reset_agent_control_for_tests() -> None:
    """Reset module state between pytest cases."""
    global _loop, _paused
    with _lock:
        _loop = None
        _paused = False


def get_trading_loop() -> Any | None:
    with _lock:
        return _loop


def is_paused() -> bool:
    with _lock:
        return _paused


def is_trading_running() -> bool:
    """True when the orchestrator loop is active and not API-paused."""
    with _lock:
        loop = _loop
        paused = _paused
    if loop is None or paused:
        return False
    try:
        return bool(loop.is_running())
    except Exception:
        return False


def enrich_tick_runtime(tick: dict[str, Any]) -> dict[str, Any]:
    """Attach live trading-loop status for dashboard / WebSocket consumers."""
    from api.agent_health import get_runtime_tick_fields

    out = dict(tick)
    out["trading_paused"] = is_paused()
    loops = is_trading_running()
    out["trading_loops_running"] = loops
    out.update(get_runtime_tick_fields())
    if "trading_healthy" not in out:
        out["trading_healthy"] = loops and not is_paused()
    return out


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
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert("⚠️ Trading loops STOPPED — no trades firing")
    except Exception as e:
        log_engine(f"telegram loop-stop alert failed: {type(e).__name__}: {e}")
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
