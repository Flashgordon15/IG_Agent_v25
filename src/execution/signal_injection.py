"""
Ad-Hoc Signal Injection Lane — thread-safe single-tick override queue.

Allows an operator to inject a synthetic MAX_CONFIDENCE signal into the
TradingLoop gate stack for a specific epic. The injection is consumed
atomically on the next tick and expires after TTL_SEC if not consumed.

Design constraints:
  - Max depth 1 per epic (overwrites, no accumulation)
  - 30-second TTL prevents stale signals from firing after market gaps
  - consume() is atomic dict.pop under lock — zero race with trading threads
  - Zero I/O, zero allocation beyond the dict entry
"""

from __future__ import annotations

import threading
import time
from typing import Any

_injection_queue: dict[str, dict[str, Any]] = {}
_injection_lock = threading.Lock()

TTL_SEC = 30.0


def enqueue_injection(epic: str, direction: str) -> dict[str, Any]:
    """Queue a signal injection. Returns the queued entry."""
    key = str(epic or "").strip()
    d = str(direction or "").strip().upper()
    if d not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got '{direction}'")
    if not key:
        raise ValueError("epic is required")

    entry = {
        "epic": key,
        "direction": d,
        "ts": time.time(),
        "source": "operator_manual",
    }
    with _injection_lock:
        _injection_queue[key] = entry
    return entry


def consume_injection(epic: str) -> dict[str, Any] | None:
    """Atomically consume and return the injection for this epic, or None."""
    key = str(epic or "").strip()
    with _injection_lock:
        entry = _injection_queue.pop(key, None)
    if entry is None:
        return None
    age = time.time() - entry.get("ts", 0)
    if age > TTL_SEC:
        return None
    return entry


def pending_injections() -> dict[str, dict[str, Any]]:
    """Snapshot of all pending injections (for GUI status display)."""
    now = time.time()
    with _injection_lock:
        expired = [k for k, v in _injection_queue.items() if now - v.get("ts", 0) > TTL_SEC]
        for k in expired:
            _injection_queue.pop(k, None)
        return {k: dict(v) for k, v in _injection_queue.items()}


def clear_injection(epic: str) -> None:
    """Remove a pending injection without consuming it."""
    key = str(epic or "").strip()
    with _injection_lock:
        _injection_queue.pop(key, None)


def clear_all() -> None:
    """Remove all pending injections."""
    with _injection_lock:
        _injection_queue.clear()
