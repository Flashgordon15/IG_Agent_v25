"""Engine diagnostics — append-only log at src/data/logs/engine.log."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from system.paths import logs_dir

_LOG = logs_dir() / "engine.log"
_ROTATE_BYTES = 10 * 1024 * 1024
_ROTATE_BACKUP = 7
_file_handler: RotatingFileHandler | None = None
_alerts_lock = threading.Lock()
_warning_count = 0
_warning_last_type: str | None = None
_intermittent_lock = threading.Lock()
_intermittent_last: dict[str, float] = {}
_intermittent_enabled: bool | None = None
_intermittent_interval_sec: float | None = None


def _pytest_isolated() -> bool:
    return os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"


def _engine_file_handler() -> RotatingFileHandler:
    global _file_handler
    if _file_handler is not None:
        return _file_handler
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        _LOG,
        maxBytes=_ROTATE_BYTES,
        backupCount=_ROTATE_BACKUP,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _file_handler = handler
    return handler


def _intermittent_settings() -> tuple[bool, float]:
    global _intermittent_enabled, _intermittent_interval_sec
    if _intermittent_enabled is not None and _intermittent_interval_sec is not None:
        return _intermittent_enabled, _intermittent_interval_sec
    enabled = True
    interval = 30.0
    try:
        from system.config_loader import ConfigLoader
        from system.paths import project_root

        cfg = ConfigLoader(project_root() / "config" / "config_v25.json").load_config()
        data = getattr(cfg, "_data", None) or {}
        if isinstance(data, dict):
            enabled = bool(data.get("engine_log_intermittent", True))
            interval = float(data.get("engine_log_intermittent_seconds", 30))
    except Exception:
        pass
    _intermittent_enabled = enabled
    _intermittent_interval_sec = max(5.0, interval)
    return _intermittent_enabled, _intermittent_interval_sec


def reset_intermittent_log_state_for_tests() -> None:
    global _intermittent_enabled, _intermittent_interval_sec
    with _intermittent_lock:
        _intermittent_last.clear()
    _intermittent_enabled = None
    _intermittent_interval_sec = None


def log_engine_intermittent(
    key: str,
    message: str,
    *,
    interval_sec: float | None = None,
    force: bool = False,
) -> bool:
    """
    Log at most once per key per interval (stream tick noise).
    Returns True if the line was written.
    """
    enabled, default_iv = _intermittent_settings()
    if not enabled:
        log_engine(message)
        return True
    iv = max(5.0, float(interval_sec if interval_sec is not None else default_iv))
    now = time.time()
    k = str(key or "default").strip() or "default"
    with _intermittent_lock:
        last = _intermittent_last.get(k, 0.0)
        if not force and now - last < iv:
            return False
        _intermittent_last[k] = now
    log_engine(message)
    return True


def log_engine(message: str) -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}"
    try:
        handler = _engine_file_handler()
        record = logging.LogRecord(
            name="ig_agent.engine_file",
            level=logging.INFO,
            pathname=str(_LOG),
            lineno=0,
            msg=line,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.flush()
    except Exception:
        pass
    if _pytest_isolated():
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
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
