"""Engine diagnostics — append-only log at src/data/logs/engine.log."""

from __future__ import annotations

from datetime import datetime

from system.paths import logs_dir

_LOG = logs_dir() / "engine.log"


def log_engine(message: str) -> None:
    from system.log_rotator import rotate_if_needed

    _LOG.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(_LOG)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n"
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    try:
        from system.logger import get_logger

        get_logger("ig_agent.engine").info(message)
    except Exception:
        pass


def clear_engine_log() -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    _LOG.write_text("", encoding="utf-8")
