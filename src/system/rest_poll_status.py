"""REST poll transport stall detection — dashboard and health telemetry."""

from __future__ import annotations

import threading
import time

from system.engine_log import log_engine

_STALL_THRESHOLD_SEC = 30.0
_TELEGRAM_AFTER_CYCLES = 3

_lock = threading.Lock()
_last_success_mono: float = 0.0
_stall_cycles: int = 0
_was_stalled: bool = False
_stall_started_mono: float = 0.0


def record_poll_success() -> None:
    """Call when a poll tick delivers at least one valid epic quote."""
    global _last_success_mono, _stall_cycles, _was_stalled, _stall_started_mono
    now = time.monotonic()
    with _lock:
        if _was_stalled:
            duration = max(0.0, now - _stall_started_mono)
            log_engine(f"[REST POLL] Recovered after {duration:.0f}s stall")
            _was_stalled = False
            _stall_cycles = 0
        _last_success_mono = now


def record_poll_cycle_without_tick() -> None:
    """Call when a full poll cycle completes with no valid quotes."""
    global _stall_cycles, _was_stalled, _stall_started_mono
    now = time.monotonic()
    with _lock:
        if not _was_stalled:
            _stall_started_mono = now
        age = now - _last_success_mono if _last_success_mono > 0 else 0.0
        if _last_success_mono > 0 and age < _STALL_THRESHOLD_SEC:
            return
        if not _was_stalled:
            _was_stalled = True
            log_engine(
                f"[REST POLL] Stall detected — no quotes for {max(age, _STALL_THRESHOLD_SEC):.0f}s"
            )
            _stall_cycles += 1
            if _stall_cycles >= _TELEGRAM_AFTER_CYCLES:
                _notify_stall_telegram(_stall_cycles)


def is_rest_poll_stalled() -> bool:
    with _lock:
        if _last_success_mono <= 0:
            return False
        return (time.monotonic() - _last_success_mono) >= _STALL_THRESHOLD_SEC


def stall_seconds() -> float:
    with _lock:
        if _last_success_mono <= 0:
            return 0.0
        return max(0.0, time.monotonic() - _last_success_mono)


def snapshot_fields() -> dict[str, object]:
    stalled = is_rest_poll_stalled()
    return {
        "rest_poll_stalled": stalled,
        "rest_poll_stall_sec": round(stall_seconds(), 1),
    }


def reset_rest_poll_status_for_tests() -> None:
    global _last_success_mono, _stall_cycles, _was_stalled, _stall_started_mono
    with _lock:
        _last_success_mono = 0.0
        _stall_cycles = 0
        _was_stalled = False
        _stall_started_mono = 0.0


def _notify_stall_telegram(cycles: int) -> None:
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier is not None:
            notifier.send_alert(
                f"⚠️ REST poll stalled — {cycles} consecutive cycles without quotes",
                dedupe_key="rest_poll_stall",
            )
    except Exception:
        pass
