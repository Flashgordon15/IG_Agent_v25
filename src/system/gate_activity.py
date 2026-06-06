"""Track recent trading-loop gate evaluation for pre-flight and watchdog checks."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_gate_eval_mono: float = 0.0
_last_gate_eval_wall: float = 0.0
_per_epic_wall: dict[str, float] = {}


def record_gate_evaluation(epic: str = "") -> None:
    """Call once per trading-loop tick where gates are evaluated."""
    global _last_gate_eval_mono, _last_gate_eval_wall
    now_mono = time.monotonic()
    now_wall = time.time()
    with _lock:
        _last_gate_eval_mono = now_mono
        _last_gate_eval_wall = now_wall
        key = str(epic or "").strip()
        if key:
            _per_epic_wall[key] = now_wall


def seconds_since_last_gate_eval() -> float | None:
    with _lock:
        if _last_gate_eval_mono <= 0:
            return None
        return time.monotonic() - _last_gate_eval_mono


def last_gate_eval_wall_time() -> float | None:
    with _lock:
        return _last_gate_eval_wall if _last_gate_eval_wall > 0 else None


def last_gate_check_by_epic() -> dict[str, float]:
    """Return epic → wall-clock timestamp of the most recent gate evaluation."""
    with _lock:
        return dict(_per_epic_wall)


def reset_gate_activity_for_tests() -> None:
    global _last_gate_eval_mono, _last_gate_eval_wall, _per_epic_wall
    with _lock:
        _last_gate_eval_mono = 0.0
        _last_gate_eval_wall = 0.0
        _per_epic_wall = {}
