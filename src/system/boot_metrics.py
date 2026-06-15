"""
Lightweight boot progress for the dashboard 2-stage launch experience.

Maps startup_tracker phase completions to operator-facing milestones
(20 / 40 / 60 / 80 / 95 / 100). Read-only, lock-friendly — safe on /api/health hot path.
"""

from __future__ import annotations

from typing import Any

# phase_id must be marked in startup_tracker before this percent is reached
_MILESTONES: tuple[tuple[str, int, str], ...] = (
    ("ig_auth", 20, "Broker Handshake"),
    ("database", 40, "Database Core"),
    ("loops", 60, "Trading Gates"),
    ("learning", 80, "Learning Plane"),
    ("test_suite", 95, "Full Test Suite"),
    ("ready", 100, "Initialization Complete"),
)


def get_boot_metrics() -> dict[str, Any]:
    """Return {percent, label, ready, stage} derived from startup_tracker (non-blocking)."""
    from system.startup_tracker import get_status

    status = get_status()
    done_ids = {p["id"] for p in status.get("phases") or [] if p.get("status") == "done"}

    percent = 0
    label = _MILESTONES[0][2]
    stage = _MILESTONES[0][0]

    for phase_id, pct, lbl in _MILESTONES:
        if phase_id in done_ids:
            percent = pct
            label = lbl
            stage = phase_id

    ready = bool(status.get("ready")) or "ready" in done_ids
    if ready:
        percent = 100
        label = "Initialization Complete"
        stage = "ready"
    else:
        for phase in status.get("phases") or []:
            if phase.get("status") == "in_progress" and phase.get("id") == "test_suite":
                label = "Full Test Suite"
                stage = "test_suite"
                percent = max(percent, 90)
                break
        else:
            for phase_id, _pct, lbl in _MILESTONES:
                if phase_id not in done_ids:
                    if phase_id not in ("test_suite", "ready") or percent <= 0:
                        label = lbl
                        stage = phase_id
                    break

    error = status.get("error")
    return {
        "percent": int(percent),
        "label": label,
        "ready": ready,
        "stage": stage,
        "error": error,
    }
