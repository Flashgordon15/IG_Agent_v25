"""
Boot progress bridge for the dashboard.

Primary source: ``SystemState`` (BootState pipeline).
Falls back to legacy ``startup_tracker`` when SystemState has not started.
"""

from __future__ import annotations

from typing import Any


def _stage_from_system_state(snap: dict[str, Any]) -> str:
    if snap.get("ready"):
        return "ready"
    phase = str(snap.get("phase") or "BOOTING").lower()
    if phase == "warming":
        return "warming"
    return phase.replace("_streaming", "_streaming")


def get_boot_metrics() -> dict[str, Any]:
    """Return {percent, label, ready, stage, system_state} for dashboard/API."""
    try:
        from system.system_state import get_system_state

        snap = get_system_state().snapshot()
        phase = str(snap.get("phase") or "").upper()
        if snap.get("error") or phase == "FAILED":
            return {
                "percent": int(snap.get("percent") or 0),
                "label": str(snap.get("phase_label") or "Boot failed"),
                "ready": False,
                "stage": "failed",
                "error": snap.get("error"),
                "system_state": snap,
            }
        try:
            from apex.warmup_progress import get_warmup_snapshot

            warm = get_warmup_snapshot()
            if warm.get("status") == "failed":
                return {
                    "percent": int(warm.get("percent") or 0),
                    "label": str(warm.get("label") or warm.get("detail") or "Warmup failed"),
                    "ready": False,
                    "stage": "failed",
                    "error": warm.get("detail"),
                    "warming": warm,
                    "system_state": snap,
                }
        except Exception:
            pass
    except Exception:
        pass

    try:
        from apex.warmup_progress import get_warmup_snapshot, is_warmup_active

        warm = get_warmup_snapshot()
        if is_warmup_active() or warm.get("status") == "warming":
            return {
                "percent": int(warm.get("percent") or 0),
                "label": str(warm.get("label") or "Compiling Vector Arrays"),
                "ready": False,
                "stage": "warming",
                "warming": warm,
                "error": None,
            }
    except Exception:
        pass

    try:
        from system.system_state import get_system_state

        snap = get_system_state().snapshot()
        if snap.get("phase") == "WARMING":
            return {
                "percent": int(snap.get("percent") or 0),
                "label": str(snap.get("phase_label") or "Compiling Vector Arrays"),
                "ready": False,
                "stage": "warming",
                "error": snap.get("error"),
                "system_state": snap,
            }
        if snap.get("started_at") or snap.get("percent", 0) > 0 or snap.get("ready"):
            return {
                "percent": int(snap.get("percent") or 0),
                "label": str(snap.get("phase_label") or "System Booting"),
                "ready": bool(snap.get("ready")),
                "stage": _stage_from_system_state(snap),
                "error": snap.get("error"),
                "system_state": snap,
            }
    except Exception:
        pass

    from system.startup_tracker import get_status

    status = get_status()
    done_ids = {p["id"] for p in status.get("phases") or [] if p.get("status") == "done"}

    _MILESTONES: tuple[tuple[str, int, str], ...] = (
        ("ig_auth", 20, "Broker Handshake"),
        ("database", 40, "Database Core"),
        ("loops", 60, "Trading Gates"),
        ("learning", 80, "Learning Plane"),
        ("test_suite", 95, "Full Test Suite"),
        ("ready", 100, "Initialization Complete"),
    )

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

    return {
        "percent": int(percent),
        "label": label,
        "ready": ready,
        "stage": stage,
        "error": status.get("error"),
    }
