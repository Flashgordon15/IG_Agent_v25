"""Process-wide QMM entry supervisor — hard-blocks all compliance gates."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_block_reason: str = ""
_block_set_at: float = 0.0


_COCKPIT_TOKEN = "COCKPIT_EMERGENCY_OVERRIDE"


def set_process_entry_block(reason: str) -> None:
    """Engage automated pause across every trading loop (in-memory only)."""
    global _block_reason, _block_set_at
    detail = str(reason or "").strip()
    if not detail or detail == _COCKPIT_TOKEN:
        return
    with _lock:
        _block_reason = detail
        _block_set_at = time.time()


def clear_process_entry_block() -> None:
    global _block_reason, _block_set_at
    with _lock:
        _block_reason = ""
        _block_set_at = 0.0


def process_entry_blocked() -> tuple[bool, str]:
    with _lock:
        if _block_reason == _COCKPIT_TOKEN:
            _block_reason = ""
            _block_set_at = 0.0
            return False, ""
        if not _block_reason:
            return False, ""
        return True, _block_reason


def process_entry_block_age_sec() -> float:
    with _lock:
        if not _block_reason or _block_set_at <= 0:
            return 0.0
        return max(0.0, time.time() - _block_set_at)


def reset_qmm_process_supervisor_for_tests() -> None:
    clear_process_entry_block()
