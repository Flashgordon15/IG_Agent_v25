"""Append-only log for DEMO mode readiness diagnostics."""

from __future__ import annotations

from datetime import datetime

from system.paths import logs_dir

_LOG = logs_dir() / "demo_readiness.log"


def log_demo_readiness(message: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n"
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def demo_readiness_log_path() -> str:
    return str(_LOG)
