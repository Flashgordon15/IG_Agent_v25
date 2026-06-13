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
    ("session_cleanup", "Previous session closed", 7),
    ("preflight", "Pre-flight checks", 14),
    ("config", "Configuration loaded", 21),
    ("ig_auth", "IG API authenticated", 35),
    ("database", "Database connected", 45),
    ("self_test", "Self-test suite passed", 53),
    ("smoke_test", "Startup smoke check passed", 60),
    ("deploy_check", "Deployment verification passed", 65),
    ("ohlc", "Market data loaded", 70),
    ("loops", "Trading loops built", 82),
    ("stream", "Live stream connected", 90),
    ("learning", "Learning plane online", 95),
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

    # Auto-complete: if the agent is already running healthy (fresh snapshot data
    # present) but startup marks were never written (e.g. agent started with old code
    # before startup_tracker existed), treat all phases as done so the splash clears.
    if "ready" not in done_snapshot:
        try:
            from api.snapshot_store import snapshot_age_s

            age = snapshot_age_s()
            if age is not None and age < 120:
                _auto_complete_all(done_snapshot, notes_snapshot)
        except Exception:
            pass

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


def _auto_complete_all(done_snapshot: dict, notes_snapshot: dict) -> None:
    """Mark every phase done in-place — used when agent is already running."""
    now = datetime.now(timezone.utc).timestamp()
    for pid in _PHASE_IDS:
        if pid not in done_snapshot:
            done_snapshot[pid] = now
    notes_snapshot.setdefault("ready", "already running")


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
