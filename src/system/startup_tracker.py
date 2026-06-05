"""
Startup phase tracker — thread-safe singleton.

Each backend phase calls mark(phase_id) when it completes.
The /api/startup/status endpoint returns get_status() so the
React StartupSplash can show real-time progress.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

# ── Phase registry ──────────────────────────────────────────────────────────
# (id, human label, cumulative % when this phase completes)
PHASES: list[tuple[str, str, int]] = [
    ("preflight", "Pre-flight checks", 8),
    ("config", "Configuration loaded", 16),
    ("ig_auth", "IG API authenticated", 35),
    ("database", "Database connected", 45),
    ("ohlc", "Market data loaded", 62),
    ("loops", "Trading loops built", 76),
    ("stream", "Live stream connected", 90),
    ("ready", "All systems ready", 100),
]

_PHASE_IDS: list[str] = [p[0] for p in PHASES]
_PHASE_PCT: dict[str, int] = {p[0]: p[2] for p in PHASES}
_PHASE_LABEL: dict[str, str] = {p[0]: p[1] for p in PHASES}

# ── Mutable state (module-level singleton) ───────────────────────────────────
_lock = threading.Lock()
_done: dict[str, float] = {}  # phase_id → unix timestamp of completion
_notes: dict[str, str | None] = {}  # phase_id → optional detail string
_error: str | None = None
_started_at: str = datetime.now(timezone.utc).isoformat(timespec="seconds")


def mark(phase_id: str, note: str | None = None) -> None:
    """Mark a phase as complete. Idempotent — safe to call multiple times."""
    if phase_id not in _PHASE_IDS:
        return
    with _lock:
        if phase_id not in _done:
            _done[phase_id] = datetime.now(timezone.utc).timestamp()
        if note is not None:
            _notes[phase_id] = note


def set_error(message: str) -> None:
    """Record a startup error (shown in the splash as a red banner)."""
    global _error
    with _lock:
        _error = message


def reset() -> None:
    """Reset all state — called on agent restart (tests only)."""
    global _error, _started_at
    with _lock:
        _done.clear()
        _notes.clear()
        _error = None
        _started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_status() -> dict[str, Any]:
    """Return JSON-serialisable status dict for /api/startup/status."""
    with _lock:
        done_snapshot = dict(_done)
        notes_snapshot = dict(_notes)
        error_snapshot = _error
        started_snapshot = _started_at

    # Find the last completed phase index to derive overall_pct
    last_done_idx = -1
    for i, pid in enumerate(_PHASE_IDS):
        if pid in done_snapshot:
            last_done_idx = i

    overall_pct = 0
    if last_done_idx >= 0:
        overall_pct = _PHASE_PCT[_PHASE_IDS[last_done_idx]]

    ready = "ready" in done_snapshot

    phases: list[dict[str, Any]] = []
    for pid, label, pct in PHASES:
        if pid in done_snapshot:
            status = "done"
        elif pid == _next_pending(done_snapshot):
            status = "in_progress"
        else:
            status = "pending"
        phases.append(
            {
                "id": pid,
                "label": label,
                "status": status,
                "note": notes_snapshot.get(pid),
                "pct": pct,
            }
        )

    return {
        "phases": phases,
        "overall_pct": overall_pct,
        "ready": ready,
        "error": error_snapshot,
        "started_at": started_snapshot,
    }


def _next_pending(done_snapshot: dict) -> str | None:
    """Return the ID of the first phase that follows the last completed one."""
    last_done_idx = -1
    for i, pid in enumerate(_PHASE_IDS):
        if pid in done_snapshot:
            last_done_idx = i
    next_idx = last_done_idx + 1
    if next_idx < len(_PHASE_IDS):
        return _PHASE_IDS[next_idx]
    return None
