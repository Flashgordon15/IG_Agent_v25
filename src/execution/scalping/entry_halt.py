"""Global entry halt after unprotected fill — thread-safe."""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

_lock = threading.Lock()
_halted: bool = False
_reason: str = ""
_halted_at: float = 0.0


def halt_entries(reason: str) -> None:
    global _halted, _reason, _halted_at
    with _lock:
        if _halted:
            return
        _halted = True
        _reason = str(reason or "entries halted")
        _halted_at = time.time()
    log_engine(f"SCALPING ENTRY HALT — {_reason}")
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(f"Entry halt: {_reason}")
    except Exception:
        pass


def is_entry_halted() -> bool:
    with _lock:
        return _halted


def entry_halt_detail() -> str:
    with _lock:
        if not _halted:
            return ""
        return _reason


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "halted": _halted,
            "reason": _reason,
            "halted_at": _halted_at,
        }


def clear_entry_halt_for_tests() -> None:
    global _halted, _reason, _halted_at
    with _lock:
        _halted = False
        _reason = ""
        _halted_at = 0.0
