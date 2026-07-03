"""Instant /health lane — never acquires readiness or iron_cage locks."""

from __future__ import annotations

import time
from typing import Any


def api_health_grace_active() -> bool:
    """True during post-bind window when heavy hydrators must not block HTTP."""
    try:
        from system.boot.api_health_grace import health_grace_active

        return health_grace_active()
    except Exception:
        return False


def build_instant_health_response() -> dict[str, Any]:
    """
    O(1) health body for bootstrap /api/health and launcher G5 probes.

    Uses health_light snapshot only — never calls build_gui_status or agent_health rebuild.
    """
    body: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "instant_lane": True,
        "hydration_grace": api_health_grace_active(),
    }
    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response()
        if hl:
            body["health_light"] = hl
            body["agent_online"] = bool(hl.get("agent_online"))
            ic = hl.get("iron_cage") if isinstance(hl.get("iron_cage"), dict) else {}
            body["trade_ready"] = bool(ic.get("trade_ready"))
    except Exception:
        pass
    try:
        from api.readiness_snapshot import resolve_gate_progression

        prog = resolve_gate_progression()
        body["gate_progression"] = prog
        body["phase"] = prog.get("phase")
        if not prog.get("warm_up_complete"):
            body["snapshot_warming"] = True
    except Exception:
        body["snapshot_warming"] = True
    body["ts"] = time.time()
    return body
