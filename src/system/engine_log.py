"""Engine diagnostics — append-only log at src/data/logs/engine.log."""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

from system.paths import logs_dir

_LOG = logs_dir() / "engine.log"
_alerts_lock = threading.Lock()
_warning_count = 0
_warning_last_type: str | None = None


def _pytest_isolated() -> bool:
    return os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"


def log_engine(message: str) -> None:
    from system.log_rotator import rotate_if_needed

    _LOG.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(_LOG)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n"
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    if _pytest_isolated():
        return
    try:
        from system.logger import get_logger

        get_logger("ig_agent.engine").info(message)
    except Exception:
        pass


def clear_engine_log() -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    _LOG.write_text("", encoding="utf-8")


def record_engine_warning(warning_type: str, message: str) -> None:
    """Increment dashboard ERRORS pill and log a visible WARNING line."""
    global _warning_count, _warning_last_type
    key = str(warning_type or "warning").strip() or "warning"
    with _alerts_lock:
        _warning_count += 1
        _warning_last_type = key
    log_engine(f"WARNING [{key}]: {message}")


def get_engine_alerts_snapshot() -> dict[str, Any]:
    """Dashboard errors payload: count + latest warning category."""
    with _alerts_lock:
        count = int(_warning_count)
        kind = _warning_last_type
    if count <= 0:
        return {"count": 0, "type": None}
    return {"count": count, "type": kind}


def reset_engine_alerts_for_tests() -> None:
    global _warning_count, _warning_last_type
    with _alerts_lock:
        _warning_count = 0
        _warning_last_type = None
